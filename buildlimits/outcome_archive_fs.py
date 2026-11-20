"""Filesystem primitives for the out-of-process outcome archiver.

Every path the archiver touches is private to its owner. Directories are
validated one component at a time so a symlinked parent cannot redirect a
write, files are opened without following links, temporary files are created
exclusively, and publication uses a no-clobber hard link followed by a
directory fsync. The run's non-blocking ``flock`` lives in
`outcome_archive_lock`; the supervisor writer never takes it and never calls
into this module's writers.
"""

import contextlib
import errno
import json
import os
import secrets
import stat
import typing as typ
from pathlib import Path

from buildlimits.outcome_store_io import fsync_directory, write_all

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_FILE_MODE = 0o600
_DIRECTORY_MODE = 0o700
_TEMP_SUFFIX = ".tmp"

__all__ = [
    "UnsafePathError",
    "atomic_write_json",
    "ensure_private_directory",
    "open_private_regular",
    "open_private_temp",
    "publish_no_clobber",
    "remove_stale_temps",
    "require_private_chain",
    "require_private_regular",
]


class UnsafePathError(OSError):
    """A path is a symlink, a non-directory, or not owned by this user."""


def ensure_private_directory(path: Path) -> None:
    """Create a private directory and refuse a symlinked path.

    Missing components are created one at a time after the component above
    them has been proven to be a real directory, and the final mode is applied
    through a descriptor opened with ``O_NOFOLLOW``. A symlink anywhere on the
    path is therefore refused before a directory is created through it or an
    existing directory outside the chain has its mode changed. A component that
    is a symlink or not a directory raises `UnsafePathError`; a filesystem
    failure propagates as `OSError`.

    Parameters
    ----------
    path : Path
        The directory to create or verify.
    """
    _create_missing_components(path)
    _make_private(path)
    require_private_chain(path)


def _components(path: Path) -> cabc.Iterator[tuple[Path, str]]:
    """Yield every component of an absolute path, refusing a relative one.

    Yields
    ------
    tuple[Path, str]
        The cumulative path and the component name that produced it.

    Raises
    ------
    UnsafePathError
        If ``path`` is not absolute.
    """
    if not path.is_absolute():
        msg = "an archive path must be absolute"
        raise UnsafePathError(msg)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        yield current, part


def _create_missing_components(path: Path) -> None:
    """Create every component of ``path`` below the anchor that is missing."""
    for current, part in _components(path):
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=_DIRECTORY_MODE)
            # ``mkdir`` is masked by the umask, so a newly created component is
            # narrowed through its own descriptor rather than assumed private.
            _make_private(current)
            continue
        _require_directory_component(part, metadata)


def _require_directory_component(part: str, metadata: os.stat_result) -> None:
    """Refuse a path component that is a symlink or not a directory.

    Raises
    ------
    UnsafePathError
        If the component is a symlink or is not a directory.
    """
    if stat.S_ISLNK(metadata.st_mode):
        msg = f"archive path component is a symlink: {part}"
        raise UnsafePathError(msg)
    if not stat.S_ISDIR(metadata.st_mode):
        msg = f"archive path component is not a directory: {part}"
        raise UnsafePathError(msg)


def _make_private(path: Path) -> None:
    """Set one directory's owner-only mode without following a link."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fchmod(descriptor, _DIRECTORY_MODE)
    finally:
        os.close(descriptor)


def require_private_chain(path: Path) -> None:
    """Reject a path whose components are symlinks or shared with others.

    A symlinked or non-directory component could redirect a write outside the
    store. So could a directory another user may rename entries in, which is
    why every component must also be private to this user: after this check an
    attacker cannot swap a checked component for a link before it is used.

    Parameters
    ----------
    path : Path
        An absolute directory path.

    Raises
    ------
    UnsafePathError
        If a component is not a directory reachable without a symlink, is
        writable by another user, or the final component is not owned by this
        user.
    """
    for current, part in _components(path):
        try:
            metadata = current.lstat()
        except FileNotFoundError as error:
            msg = f"archive path component is missing: {part}"
            raise UnsafePathError(msg) from error
        _require_directory_component(part, metadata)
        _require_private_directory(metadata, part)
    final = path.lstat()
    if final.st_uid != os.getuid():
        msg = "an archive directory must be owned by this user"
        raise UnsafePathError(msg)


def _require_private_directory(metadata: os.stat_result, part: str) -> None:
    """Reject a directory another user could replace entries in.

    A root-owned directory with the sticky bit set is the one accepted
    exception. The sticky bit stops another user removing or renaming an entry
    this account owns, which is what makes a shared system temporary directory
    safe to place a private store under.

    Parameters
    ----------
    metadata : os.stat_result
        The result of ``lstat`` on one path component.
    part : str
        The component name, used in the error message.

    Raises
    ------
    UnsafePathError
        If the directory is writable by its group or by every user and is not
        a trusted root-owned sticky directory.
    """
    if not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return
    is_trusted_shared = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
    if not is_trusted_shared:
        msg = f"archive path component is writable by another user: {part}"
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
        require_private_regular(metadata)
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
        require_private_regular(os.fstat(descriptor))
        os.fchmod(descriptor, _FILE_MODE)
    except OSError:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return descriptor, path


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


def require_private_regular(metadata: os.stat_result) -> None:
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
