"""Internal bounded file operations for cache-outcome storage.

These shared filesystem primitives are package implementation support for the
store writer and salt adapter, not a standalone public API.
"""

import contextlib
import json
import os
import stat
import typing as typ

from buildlimits import outcome_records

if typ.TYPE_CHECKING:
    import collections.abc as cabc
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
    descriptor, metadata = open_private_regular(path, flags, enforce_mode=False)
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
    temporary = path.with_name(f".{path.name}.new{_TEMP_SUFFIX}")
    temporary.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, _FILE_MODE)
    write_private_temp(temporary, descriptor, document)
    return temporary


def seal_active(active: Path, destination: Path) -> None:
    """Rename the active store into the sealed directory and sync both.

    Filesystem failures propagate as ``OSError``; the caller reports them as a
    failed seal and retries after the next append.
    """
    active.replace(destination)
    fsync_directory(destination.parent)
    fsync_directory(active.parent)


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


def _replace_document(path: Path, data: bytes) -> None:
    """Write bytes through one private temporary file and replace ``path``."""
    temporary = path.with_name(f".{path.name}{_TEMP_SUFFIX}")
    temporary.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, _FILE_MODE)
    write_private_temp(temporary, descriptor, data)
    replace_temp(temporary, path)
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
