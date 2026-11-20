"""Internal store-document operations for cache-outcome storage.

These operations write and replace whole store documents and their sidecars.
The private-file primitives they build on, including the bounded reader, live
in :mod:`buildlimits.outcome_store_files`. Both are package implementation
support for the store writer and salt adapter, not a standalone public API.
"""

import contextlib
import json
import os
import typing as typ

from buildlimits import outcome_records
from buildlimits.outcome_store_files import (
    FILE_MODE,
    OversizedFileError,
    open_private_regular,
    read_private_bytes,
    require_regular,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreRecord

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


def read_store(
    path: Path,
) -> outcome_records.DecodedStore | outcome_records.RecordFailure:
    """Read a private JSON Lines file; filesystem failures propagate as OSError.

    Returns
    -------
    DecodedStore or RecordFailure
        Validated store rows or the closed corruption/version result.

    """
    try:
        document = read_private_bytes(path, limit=outcome_records.MAX_STORE_BYTES)
    except OversizedFileError:
        return outcome_records.RecordFailure("corrupt")
    try:
        complete_document = document[: document.rfind(b"\n") + 1]
        # An interrupted append can end inside a multibyte character. Discard
        # that unterminated byte sequence before strict UTF-8 decoding.
        return outcome_records.decode_lines(complete_document.decode("utf-8"))
    except UnicodeError:
        return outcome_records.RecordFailure("corrupt")


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
    require_regular(metadata)
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


def write_private_temp(temporary: Path, descriptor: int, data: bytes) -> None:
    """Write, sync and close one already-created private temporary file.

    Raises
    ------
    OSError
        If writing, syncing or closing fails; the temporary file is removed.
    """
    try:
        os.fchmod(descriptor, FILE_MODE)
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


def write_sequence(path: Path, value: int) -> None:
    """Atomically store the next sealed sequence number.

    The counter is written before a segment is moved, so a crash leaves a gap
    the inventory reports rather than a sequence two segments can share.
    Filesystem failures propagate as ``OSError``.
    """
    _replace_document(path, f"{value}\n".encode("ascii"))


def write_metadata(path: Path, document: cabc.Mapping[str, object]) -> None:
    """Atomically store one JSON metadata document beside a sealed source.

    Filesystem failures propagate as ``OSError``.
    """
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    _replace_document(path, encoded + b"\n")


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
    descriptor = os.open(temporary, flags, FILE_MODE)
    write_private_temp(temporary, descriptor, data)
    return temporary


def _replace_document(path: Path, data: bytes) -> None:
    """Write bytes through one private temporary file and replace ``path``."""
    replace_temp(write_private_temporary(path, data), path)
    fsync_directory(path.parent)


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
