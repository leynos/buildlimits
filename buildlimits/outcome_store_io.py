"""Internal bounded file operations for cache-outcome storage.

These shared filesystem primitives are package implementation support for the
store writer and salt adapter, not a standalone public API.
"""

import contextlib
import dataclasses as dc
import errno
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


def append_line(path: Path, line: bytes) -> None:
    """Append one encoded record and roll back a partial final write.

    Raises
    ------
    OSError
        If the file cannot be opened or the append cannot be completed.
    """
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor, metadata = open_private_regular(path, flags)
    try:
        try:
            write_all(descriptor, line + b"\n")
        except OSError:
            with contextlib.suppress(OSError):
                os.ftruncate(descriptor, metadata.st_size)
            raise
    finally:
        os.close(descriptor)


class _OversizedError(OSError):
    """A private file grew past the caller's read bound."""


@dc.dataclass(frozen=True, slots=True)
class StoreSnapshot:
    """One bounded read of a store, with the size of the bytes it decoded.

    Attributes
    ----------
    logical_bytes : int
        Bytes read from the file, header and every terminating newline
        included. A torn final fragment is part of the file's size even though
        it contributes no row.
    decoded : DecodedStore or RecordFailure
        Validated rows, or the closed reason the bytes could not be decoded.
    """

    logical_bytes: int
    decoded: outcome_records.DecodedStore | outcome_records.RecordFailure


def read_store(
    path: Path,
) -> outcome_records.DecodedStore | outcome_records.RecordFailure:
    """Read a private JSON Lines file; filesystem failures propagate as OSError.

    Returns
    -------
    DecodedStore or RecordFailure
        Validated store rows or the closed corruption/version result.

    """
    return read_store_snapshot(path).decoded


def read_store_snapshot(path: Path) -> StoreSnapshot:
    """Read a private JSON Lines file once, returning its bytes and its rows.

    Measuring the size separately would let a concurrent append land between
    the two calls, reporting the old size beside the new row count. One read
    keeps both figures describing the same document, which is what a
    diagnostic that compares them needs.

    Returns
    -------
    StoreSnapshot
        The decoded rows or closed failure, with the exact byte length read.
        A filesystem failure propagates as ``OSError`` rather than becoming a
        snapshot: an unreadable store is not an empty one.
    """
    try:
        document = read_private_bytes(path, limit=outcome_records.MAX_STORE_BYTES)
    except _OversizedError:
        # The rows are untrusted, but the file's size is still a fact. Only a
        # failing ``lstat`` here would be an unreadable store.
        return StoreSnapshot(store_size(path), outcome_records.RecordFailure("corrupt"))
    try:
        complete_document = document[: document.rfind(b"\n") + 1]
        # An interrupted append can end inside a multibyte character. Discard
        # that unterminated byte sequence before strict UTF-8 decoding.
        decoded = outcome_records.decode_lines(complete_document.decode("utf-8"))
    except UnicodeError:
        decoded = outcome_records.RecordFailure("corrupt")
    return StoreSnapshot(len(document), decoded)


def read_private_bytes(path: Path, *, limit: int) -> bytes:
    """Read one private regular file entirely, refusing anything over ``limit``.

    The file is opened without following links and its size is checked both
    from the descriptor's metadata and from the bytes actually read, so a file
    that grows while it is read cannot exceed the caller's bound either.
    Filesystem failures other than an oversized file propagate as ``OSError``.

    Parameters
    ----------
    path : Path
        The private regular file to read.
    limit : int
        The largest permitted size in bytes.

    Returns
    -------
    bytes
        The file's contents.

    Raises
    ------
    _OversizedError
        If the file's size, measured either way, exceeds ``limit``.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor, metadata = open_private_regular(path, flags, enforce_mode=False)
    if metadata.st_size > limit:
        os.close(descriptor)
        raise _OversizedError(errno.EFBIG, "file exceeds its read bound")
    with os.fdopen(descriptor, "rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise _OversizedError(errno.EFBIG, "file exceeds its read bound")
    return data


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
