"""File primitives for the out-of-process outcome archiver.

Every path the archiver touches is private to its owner. Files are opened
without following links, temporary files are created exclusively, and
publication uses a no-clobber hard link followed by a directory fsync. The
directory walk that proves every parent is private lives in
`outcome_archive_dirs` and is re-exported here, so callers keep one import.
The run's non-blocking ``flock`` lives in `outcome_archive_lock`; the
supervisor writer never takes it and never calls into this module's writers.
"""

import contextlib
import errno
import json
import os
import secrets
import stat
import typing as typ

from buildlimits.outcome_archive_dirs import (
    UnsafePathError,
    ensure_private_directory,
    open_private_chain_when_present,
    require_private_chain,
)
from buildlimits.outcome_store_files import open_private_regular as _open_private
from buildlimits.outcome_store_io import fsync_directory, write_all

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_FILE_MODE = 0o600
_TEMP_SUFFIX = ".tmp"

__all__ = [
    "UnsafePathError",
    "atomic_write_json",
    "ensure_private_directory",
    "open_private_regular",
    "open_private_temp",
    "publish_no_clobber",
    "remove_maintenance_files",
    "remove_private_files",
    "remove_stale_temps",
    "require_private_chain",
    "require_private_regular",
]


def open_private_regular(
    path: Path,
    flags: int,
    *,
    enforce_mode: bool = True,
) -> tuple[int, os.stat_result]:
    """Open a private regular file without following links.

    The open itself is the outcome store's primitive, so the no-follow and
    non-blocking flags, the close-on-failure rule and the race-free narrowing
    exist in exactly one place and the archiver cannot drift from them. This
    wrapper supplies the archiver's stricter validation: a file another
    account owns is not private, whatever its mode bits say.

    Parameters
    ----------
    path : Path
        The file to open.
    flags : int
        Open flags; ``O_NOFOLLOW`` and ``O_NONBLOCK`` are added.
    enforce_mode : bool, default=True
        Whether to narrow the file to the private owner-only mode.

    Returns
    -------
    tuple[int, os.stat_result]
        The open descriptor and its metadata. A file that cannot be opened,
        inspected or made private propagates the ``OSError`` the shared
        primitive raises.
    """
    return _open_private(
        path,
        flags,
        enforce_mode=enforce_mode,
        validate=require_private_regular,
    )


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


def remove_private_files(directory: Path, names: cabc.Iterable[str]) -> None:
    """Remove named files through one validated directory descriptor.

    The chain is validated once, and every removal is then relative to that
    same descriptor, so a path replaced after validation cannot redirect the
    deletions outside the store. The descriptor is synced before it closes, so
    the removals survive a crash. A name that is already absent is not an
    error: an earlier run may have removed it before stopping, and reporting
    that as a failure would strand the run's remaining work.

    Parameters
    ----------
    directory : Path
        The private directory holding the files.
    names : collections.abc.Iterable[str]
        Names of files inside the directory.

    Notes
    -----
    A chain that is not private to this user is refused with `UnsafePathError`
    from `open_private_chain_when_present`, and a removal or sync that fails
    propagates its `OSError`. A directory that does not exist is treated as
    holding no files, so this is the removal equivalent of a ``missing_ok``
    unlink.
    """
    descriptor = open_private_chain_when_present(directory)
    if descriptor is None:
        return
    try:
        for name in names:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
    return _remove_matching(
        directory,
        lambda name: name.endswith(_TEMP_SUFFIX),
        tolerate_failures=True,
    )


def _remove_matching(
    directory: Path,
    matches: cabc.Callable[[str], bool],
    *,
    tolerate_failures: bool = False,
) -> tuple[str, ...]:
    """Remove the regular files one predicate names, through one descriptor.

    Parameters
    ----------
    directory : Path
        The directory to clean.
    matches : Callable[[str], bool]
        Whether one file name is abandoned work.
    tolerate_failures : bool
        Whether one file that cannot be removed is skipped. A cleanup that only
        tidies abandoned work sets this; one whose result a caller relies on
        lets the failure propagate.

    Returns
    -------
    tuple[str, ...]
        Names that were removed; a listing, removal or sync failure propagates.
    """
    descriptor = open_private_chain_when_present(directory)
    if descriptor is None:
        return ()
    try:
        with os.scandir(descriptor) as iterator:
            removed = [
                entry.name
                for entry in iterator
                if matches(entry.name)
                and _remove_entry(
                    descriptor, entry, tolerate_failures=tolerate_failures
                )
            ]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return tuple(removed)


def _remove_entry(
    descriptor: int,
    entry: os.DirEntry[str],
    *,
    tolerate_failures: bool,
) -> bool:
    """Remove one listed entry, reporting whether it is now gone.

    Returns
    -------
    bool
        True when this call removed the entry.

    Raises
    ------
    OSError
        If the entry cannot be removed and ``tolerate_failures`` is false.
    """
    try:
        metadata = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        return False
    try:
        os.unlink(entry.name, dir_fd=descriptor)
    except FileNotFoundError:
        return False
    except OSError:
        if not tolerate_failures:
            raise
        return False
    return True


def remove_maintenance_files(directory: Path, prefix: str) -> tuple[str, ...]:
    """Remove abandoned maintenance temporaries under the archive lock.

    The sealed directory is shared with the writer, which publishes metadata
    through its own temporary files while a seal is in progress. Only names
    carrying the maintenance prefix are removed, so a concurrent seal can never
    lose the file it is about to install.

    Parameters
    ----------
    directory : Path
        The directory whose abandoned maintenance files are removed.
    prefix : str
        The maintenance prefix, matched as ``.<prefix>-``.

    Returns
    -------
    tuple[str, ...]
        Names that were removed.

    Notes
    -----
    A missing directory holds nothing. A refused chain or a failed removal or
    sync propagates as `OSError`.
    """
    marker = f".{prefix}-"
    return _remove_matching(
        directory,
        lambda name: name.startswith(marker) and name.endswith(_TEMP_SUFFIX),
    )


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


def is_proven_absent(path: Path) -> bool:
    """Return whether a lookup proves the path does not exist.

    ``Path.exists`` answers ``False`` for every ``OSError``, so it cannot tell
    an absent file from one the process may not inspect. Only a lookup that
    names the file as missing is evidence of absence; any other failure leaves
    the answer unknown.

    Returns
    -------
    bool
        Whether the path is confirmed absent.
    """
    try:
        path.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False
