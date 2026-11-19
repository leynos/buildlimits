"""Filesystem primitives for the out-of-process outcome archiver.

Every path the archiver touches is private to its owner. Directories are
validated one component at a time so a symlinked parent cannot redirect a
write, files are opened without following links, temporary files are created
exclusively, and publication uses a no-clobber hard link followed by a
directory fsync. The archive lock is a non-blocking exclusive ``flock``; the
supervisor writer never takes it and never calls into this module's writers.
"""

import contextlib
import errno
import fcntl
import json
import os
import secrets
import stat
import typing as typ
from pathlib import Path

from buildlimits.outcome_store_io import write_all

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_FILE_MODE = 0o600
_DIRECTORY_MODE = 0o700
_TEMP_SUFFIX = ".tmp"

__all__ = [
    "ArchiveBusyError",
    "ArchiveLock",
    "NoHeadroomError",
    "UnsafePathError",
    "atomic_write_json",
    "ensure_private_directory",
    "fsync_directory",
    "open_private_regular",
    "open_private_temp",
    "publish_no_clobber",
    "remove_stale_temps",
    "require_headroom",
    "require_private_chain",
]


class ArchiveBusyError(Exception):
    """Another archiver holds the non-blocking lock."""


class UnsafePathError(OSError):
    """A path is a symlink, a non-directory, or not owned by this user."""


class NoHeadroomError(OSError):
    """The filesystem cannot hold the temporary files a run needs."""


class ArchiveLock:
    """A non-blocking exclusive lock around one archive directory.

    Parameters
    ----------
    path : Path
        The lock file inside the archive directory.

    Examples
    --------
    ``with ArchiveLock(path):`` raises ``ArchiveBusyError`` instead of waiting
    when a second invocation already holds the lock.
    """

    def __init__(self, path: Path) -> None:
        """Remember the lock file; nothing is opened until entry."""
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> ArchiveLock:
        """Take the lock or report that another run holds it.

        Returns
        -------
        ArchiveLock
            The held lock.

        Raises
        ------
        ArchiveBusyError
            If another process holds the lock.
        OSError
            If the lock file cannot be opened, inspected or locked.
        """
        ensure_private_directory(self._path.parent)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(self._path, flags, _FILE_MODE)
        try:
            metadata = os.fstat(descriptor)
            _require_private_regular(metadata)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise ArchiveBusyError from error
        except OSError:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: object,
    ) -> None:
        """Release the lock and close its descriptor."""
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def ensure_private_directory(path: Path) -> None:
    """Create a private directory and refuse a symlinked path.

    Parameters
    ----------
    path : Path
        The directory to create or verify.
    """
    path.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
    require_private_chain(path)
    path.chmod(_DIRECTORY_MODE)


def require_private_chain(path: Path) -> None:
    """Reject a path whose components are symlinks or non-directories.

    Parameters
    ----------
    path : Path
        An absolute directory path.

    Raises
    ------
    UnsafePathError
        If a component is not a directory reachable without a symlink, or the
        final component is not owned by this user.
    """
    if not path.is_absolute():
        msg = "an archive path must be absolute"
        raise UnsafePathError(msg)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError as error:
            msg = f"archive path component is missing: {part}"
            raise UnsafePathError(msg) from error
        if stat.S_ISLNK(metadata.st_mode):
            msg = f"archive path component is a symlink: {part}"
            raise UnsafePathError(msg)
        if not stat.S_ISDIR(metadata.st_mode):
            msg = f"archive path component is not a directory: {part}"
            raise UnsafePathError(msg)
    final = path.lstat()
    if final.st_uid != os.getuid():
        msg = "an archive directory must be owned by this user"
        raise UnsafePathError(msg)


def open_private_regular(
    path: Path,
    flags: int,
    *,
    mode: int = _FILE_MODE,
    enforce_mode: bool = True,
) -> tuple[int, os.stat_result]:
    """Open a private regular file without following links.

    Parameters
    ----------
    path : Path
        The file to open.
    flags : int
        Open flags; ``O_NOFOLLOW`` and ``O_NONBLOCK`` are added.
    mode : int, default=0o600
        Mode applied when ``enforce_mode`` is true.
    enforce_mode : bool, default=True
        Whether to require and set the private owner-only mode.

    Returns
    -------
    tuple[int, os.stat_result]
        The open descriptor and its metadata.

    Raises
    ------
    OSError
        If the file cannot be opened, inspected or made private.
    """
    full_flags = flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, full_flags)
    try:
        metadata = os.fstat(descriptor)
        _require_private_regular(metadata)
        if enforce_mode:
            os.fchmod(descriptor, mode)
    except OSError:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise
    return descriptor, metadata


def open_private_temp(directory: Path, *, prefix: str) -> tuple[int, Path]:
    """Create one exclusive private temporary file.

    Parameters
    ----------
    directory : Path
        Destination directory, used directly so publication is same-filesystem.
    prefix : str
        A bounded, caller-chosen name prefix.

    Returns
    -------
    tuple[int, Path]
        The open descriptor and the temporary path.

    Raises
    ------
    OSError
        If the temporary file cannot be created or made private.
    """
    name = f".{prefix}.{secrets.token_hex(8)}{_TEMP_SUFFIX}"
    path = directory / name
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags, _FILE_MODE)
    try:
        _require_private_regular(os.fstat(descriptor))
        os.fchmod(descriptor, _FILE_MODE)
    except OSError:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return descriptor, path


def fsync_directory(path: Path) -> None:
    """Make a directory's entries durable.

    Parameters
    ----------
    path : Path
        The directory to sync.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_headroom(directory: Path, required_bytes: int) -> None:
    """Refuse a run that cannot hold its temporary files.

    Parameters
    ----------
    directory : Path
        The filesystem whose free space must cover ``required_bytes``.
    required_bytes : int
        Temporary bytes the run may need.

    Raises
    ------
    NoHeadroomError
        If the filesystem's available bytes are below the requirement.
    """
    statistics = os.statvfs(directory)
    available = statistics.f_bavail * statistics.f_frsize
    if available < required_bytes:
        msg = "insufficient temporary disk headroom"
        raise NoHeadroomError(msg)


def publish_no_clobber(temporary: Path, destination: Path) -> None:
    """Publish one completed file without replacing an existing one.

    Parameters
    ----------
    temporary : Path
        The verified file in the destination directory.
    destination : Path
        The final name.

    Raises
    ------
    OSError
        If linking or the directory sync fails; the temporary file is kept.
    """
    os.link(temporary, destination)
    try:
        fsync_directory(destination.parent)
    except OSError:
        with contextlib.suppress(OSError):
            destination.unlink()
        raise


def atomic_write_json(path: Path, document: cabc.Mapping[str, object]) -> None:
    """Write one JSON document durably by replacement.

    Parameters
    ----------
    path : Path
        The destination file.
    document : collections.abc.Mapping[str, object]
        A JSON-compatible mapping.

    Raises
    ------
    OSError
        If the temporary file cannot be written or published.
    """
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    descriptor, temporary = open_private_temp(path.parent, prefix=path.name)
    try:
        write_all(descriptor, encoded + b"\n")
        os.fsync(descriptor)
    except OSError:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    os.close(descriptor)
    try:
        temporary.replace(path)
        fsync_directory(path.parent)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def remove_stale_temps(directory: Path) -> tuple[str, ...]:
    """Remove abandoned private temporary files under the archive lock.

    Parameters
    ----------
    directory : Path
        The directory whose ``.tmp`` entries are abandoned.

    Returns
    -------
    tuple[str, ...]
        Names that were removed.
    """
    removed: list[str] = []
    try:
        iterator = os.scandir(directory)
    except OSError:
        return ()
    try:
        for entry in iterator:
            if not entry.name.endswith(_TEMP_SUFFIX):
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            try:
                Path(entry.path).unlink()
            except OSError:
                continue
            removed.append(entry.name)
    finally:
        iterator.close()
    return tuple(removed)


def _require_private_regular(metadata: os.stat_result) -> None:
    """Reject an object that is not a private regular file.

    Parameters
    ----------
    metadata : os.stat_result
        The result of ``fstat`` on the opened object.

    Raises
    ------
    UnsafePathError
        If the object is not a regular file or is not owned by this user.
    """
    if not stat.S_ISREG(metadata.st_mode):
        msg = "archive path is not a regular file"
        raise UnsafePathError(msg, errno.EINVAL)
    if metadata.st_uid != os.getuid():
        msg = "archive file is not owned by this user"
        raise UnsafePathError(msg, errno.EPERM)
