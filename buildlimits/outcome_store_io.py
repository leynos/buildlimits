"""Internal bounded file operations for cache-outcome storage.

These shared filesystem primitives are package implementation support for the
store writer and salt adapter, not a standalone public API.
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
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor, metadata = open_private_regular(path, flags)
    if metadata.st_size > outcome_records.MAX_STORE_BYTES:
        os.close(descriptor)
        return outcome_records.RecordFailure("corrupt")
    with os.fdopen(descriptor, "rb") as source:
        document = source.read(outcome_records.MAX_STORE_BYTES + 1)
    if len(document) > outcome_records.MAX_STORE_BYTES:
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
    temporary = path.with_name(f".{path.name}{_TEMP_SUFFIX}")
    temporary.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, _FILE_MODE)
    write_private_temp(temporary, descriptor, document)
    replace_temp(temporary, path)


def open_private_regular(path: Path, flags: int) -> tuple[int, os.stat_result]:
    """Open a regular file without following links and enforce private mode.

    Returns
    -------
    tuple[int, os.stat_result]
        The open descriptor and the metadata captured from it.

    Raises
    ------
    OSError
        If opening, validating or changing the file mode fails.
    """
    descriptor = os.open(path, flags | getattr(os, "O_NONBLOCK", 0))
    try:
        metadata = os.fstat(descriptor)
        _require_regular(metadata)
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
