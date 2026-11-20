"""Write-side and durable file operations for cache-outcome storage.

Appending a row, replacing the active document, installing a temporary file and
keeping a rename durable all live here, together with the regular-file checks
the bounded reads in ``outcome_store_read`` share. These are package
implementation support for the store writer and salt adapter, not a standalone
public API.
"""

import contextlib
import os
import stat
import typing as typ

from buildlimits import outcome_records

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreRecord

_FILE_MODE = 0o600
_DIRECTORY_MODE = 0o700
_TEMP_SUFFIX = ".tmp"


class TornTailError(OSError):
    """A refused append left bytes on disk that the caller must repair.

    Raised when a partial line reached the file and the rollback that should
    have removed it failed. The caller's rows no longer account for the file's
    bytes, so it must not report the measured size beside its row count as one
    state until a compaction rewrites the file.
    """


def append_line(path: Path, line: bytes) -> None:
    """Append one encoded record and roll back a partial final write.

    Whether the append succeeded is decided by the write alone. A close that
    fails after the whole line reached the file must not turn a landed row
    into a refused one: the caller would keep a row list shorter than the
    file, and its next compaction would drop the row it never counted. The
    same suppression keeps a close failure from replacing the error of a
    write that did fail, which is the one the caller must act on.

    Raises
    ------
    TornTailError
        If the line could not be written whole and the rollback that removes
        the fragment also failed.
    OSError
        If the file cannot be opened or the line cannot be written whole.
    """
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor, metadata = open_private_regular(path, flags)
    try:
        write_all(descriptor, line + b"\n")
    except OSError as fault:
        if not _roll_back(descriptor, metadata.st_size):
            msg = "a refused append left a partial line on disk"
            raise TornTailError(msg) from fault
        raise
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)


def _roll_back(descriptor: int, size: int) -> bool:
    """Truncate a refused append back to the size the file had before it.

    Returns
    -------
    bool
        Whether the partial line was removed.
    """
    try:
        os.ftruncate(descriptor, size)
    except OSError:
        return False
    return True


def store_size(path: Path) -> int:
    """Return a regular store's size or zero; an invalid path raises OSError.

    Returns
    -------
    int
        The regular file size, or zero when the file does not exist.

    """
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return 0
    _require_regular(metadata)
    return metadata.st_size


def replace_store(
    path: Path,
    header: outcome_records.StoreHeader,
    records: tuple[StoreRecord, ...],
) -> None:
    """Write a compacted document; filesystem failures propagate as OSError.

    Raises
    ------
    ValueError
        If the compacted bytes exceed the retention bound.
    """
    lines = [outcome_records.encode_line(header)]
    lines.extend(outcome_records.encode_line(record) for record in records)
    document = ("\n".join(lines) + "\n").encode("utf-8")
    if len(document) > outcome_records.MAX_COMPACT_BYTES:
        msg = "compacted document exceeds its byte bound"
        raise ValueError(msg)
    replace_temp(write_private_temporary(path, document), path)


def open_private_regular(
    path: Path, flags: int, *, enforce_mode: bool = True
) -> tuple[int, os.stat_result]:
    """Open a regular file without following links, optionally enforcing mode.

    Returns
    -------
    tuple[int, os.stat_result]
        The open descriptor and the metadata captured from it.

    Raises
    ------
    OSError
        If opening, validating or enforcing the private file mode fails.
    """
    descriptor = os.open(path, flags | getattr(os, "O_NONBLOCK", 0))
    try:
        metadata = os.fstat(descriptor)
        _require_regular(metadata)
        if enforce_mode:
            os.fchmod(descriptor, _FILE_MODE)
    except OSError:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise
    return descriptor, metadata


def _require_regular(metadata: os.stat_result) -> None:
    """Reject filesystem objects that are not regular files.

    Raises
    ------
    OSError
        If the supplied metadata describes another filesystem object.
    """
    if not stat.S_ISREG(metadata.st_mode):
        msg = "outcome store is not a regular file"
        raise OSError(msg)


def write_private_temp(temporary: Path, descriptor: int, data: bytes) -> None:
    """Write, sync and close one already-created private temporary file.

    Raises
    ------
    OSError
        If writing, syncing or closing fails; the temporary file is removed.
    """
    try:
        os.fchmod(descriptor, _FILE_MODE)
        write_all(descriptor, data)
        os.fsync(descriptor)
    except OSError:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    try:
        os.close(descriptor)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def replace_temp(temporary: Path, destination: Path) -> None:
    """Atomically install one completed temporary file.

    Raises
    ------
    OSError
        If replacement fails; the temporary file is removed.
    """
    try:
        temporary.replace(destination)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def prepare_store(path: Path, header: outcome_records.StoreHeader) -> Path:
    """Write a header-only store beside ``path`` without installing it.

    Returns
    -------
    Path
        The synced temporary file the caller may rename into place.
    """
    document = (outcome_records.encode_line(header) + "\n").encode("utf-8")
    return write_private_temporary(path, document, suffix=".new")


def move_aside(path: Path) -> bool:
    """Move a rejected store to its ``.bad`` sibling without replacing one.

    Parameters
    ----------
    path : Path
        The unreadable store.

    Returns
    -------
    bool
        Whether the file was moved aside. ``False`` means a backup already
        exists or the rename failed, so the rejected data must be reported
        rather than silently overwritten.
    """
    backup = path.with_name(f"{path.name}.bad")
    try:
        backup.lstat()
    except FileNotFoundError:
        pass
    else:
        return False
    try:
        path.replace(backup)
    except OSError:
        return False
    return True


def sync_rename(first: Path, second: Path) -> None:
    """Make a completed cross-directory rename durable in both directories.

    Filesystem failures propagate as ``OSError``. The rename itself has
    already happened, so the caller reports the durability fault without
    pretending the rows are still active.
    """
    fsync_directory(first)
    fsync_directory(second)


def ensure_directory(path: Path) -> None:
    """Create a private directory if it does not exist.

    Filesystem failures propagate as ``OSError``.
    """
    path.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
    path.chmod(_DIRECTORY_MODE)


def fsync_directory(path: Path) -> None:
    """Make a directory's entries durable.

    Filesystem failures propagate as ``OSError``.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private_temporary(path: Path, data: bytes, *, suffix: str = "") -> Path:
    """Write ``data`` into a new private temporary beside ``path``.

    Parameters
    ----------
    path : Path
        The destination the temporary will later replace.
    data : bytes
        The complete contents to write.
    suffix : str
        An extra infix before the temporary suffix, used when a caller needs a
        name distinct from the one a replacement uses.

    Returns
    -------
    Path
        The synced temporary file; the caller installs or removes it.
    """
    temporary = path.with_name(f".{path.name}{suffix}{_TEMP_SUFFIX}")
    temporary.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, _FILE_MODE)
    write_private_temp(temporary, descriptor, data)
    return temporary


def write_all(descriptor: int, data: bytes) -> None:
    """Write all bytes or raise when the operating system refuses progress.

    Raises
    ------
    OSError
        If the write is refused or makes no progress.
    """
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            msg = "file write made no progress"
            raise OSError(msg)
        remaining = remaining[written:]
