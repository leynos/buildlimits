"""Streaming XZ write, scan and verification passes for one segment.

A segment is read once to write its archive and once again, decompressed, to
prove the archive is what the source described. Both passes keep one chunk and
one row in memory, so peak use depends on the XZ preset, not on the segment's
size. A malformed row, an unsupported header, a truncated stream or trailing
garbage is a closed fault the caller quarantines or retries.
"""

import dataclasses as dc
import hashlib
import lzma
import typing as typ

from buildlimits import outcome_record_codec
from buildlimits.outcome_record_format import RecordFailure, StoreHeader, StoreRecord
from buildlimits.outcome_record_stream import LineStream, StreamFaultError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

type FaultReason = typ.Literal[
    "corrupt",
    "version_mismatch",
    "truncated_xz",
    "trailing_data",
    "bad_xz",
    "line_too_long",
]

__all__ = [
    "ArchiveFacts",
    "DocumentReader",
    "HeaderRow",
    "RecordRow",
    "SegmentFaultError",
    "Trim",
    "scan_archive",
    "write_archive",
]


class SegmentFaultError(Exception):
    """A segment that cannot be archived or verified faithfully.

    Parameters
    ----------
    reason : FaultReason
        Closed classification used in reports and quarantine notes.
    """

    def __init__(self, reason: FaultReason) -> None:
        """Retain the closed reason as the exception message."""
        super().__init__(reason)
        self.reason = reason


@dc.dataclass(frozen=True, slots=True)
class HeaderRow:
    """One decoded header row with its original bytes."""

    raw: bytes
    header: StoreHeader


@dc.dataclass(frozen=True, slots=True)
class RecordRow:
    """One decoded record row with its original bytes."""

    raw: bytes
    record: StoreRecord


type DocumentRow = HeaderRow | RecordRow


@dc.dataclass(frozen=True, slots=True)
class ArchiveFacts:
    """What one archive or source pass proved about a segment.

    Attributes
    ----------
    salt_id : str
        The record-domain identifier from the header.
    record_count : int
        Non-header rows represented.
    logical_bytes : int
        Uncompressed bytes, including newlines and the header.
    sha256 : str
        Digest of the uncompressed content.
    first_observed_at : datetime | None
        Earliest represented row observation.
    last_observed_at : datetime | None
        Latest represented row observation.
    had_torn_tail : bool
        Whether an unusable final fragment was dropped.
    """

    salt_id: str
    record_count: int
    logical_bytes: int
    sha256: str
    first_observed_at: dt.datetime | None
    last_observed_at: dt.datetime | None
    had_torn_tail: bool

    def matches(self, other: ArchiveFacts) -> bool:
        """Compare the fields that must survive compression unchanged.

        Parameters
        ----------
        other : ArchiveFacts
            The facts to compare against.

        Returns
        -------
        bool
            Whether count, logical size and digest agree.
        """
        return (
            self.record_count == other.record_count
            and self.logical_bytes == other.logical_bytes
            and self.sha256 == other.sha256
        )


@dc.dataclass(frozen=True, slots=True)
class Trim:
    """A prefix trim applied while a segment is rewritten.

    Attributes
    ----------
    before : datetime | None
        Drop rows observed strictly before this time.
    records : int
        Drop this many leading rows regardless of time.
    allowed_bytes : int | None
        Keep at most this many logical bytes, dropping leading rows.
    source_bytes : int
        Original logical size, needed to honour ``allowed_bytes`` exactly.
    """

    before: dt.datetime | None = None
    records: int = 0
    allowed_bytes: int | None = None
    source_bytes: int = 0

    @property
    def is_empty(self) -> bool:
        """Whether the trim would keep every row.

        Returns
        -------
        bool
            Whether all trim fields are inert.
        """
        return self.before is None and self.records == 0 and self.allowed_bytes is None


class DocumentReader:
    """Decode one line stream into typed rows, tolerating only a torn tail.

    Parameters
    ----------
    stream : LineStream
        The plain or decompressed line source.

    Attributes
    ----------
    header : StoreHeader | None
        Set once the first row is read.
    had_torn_tail : bool
        Whether an unusable final fragment was dropped.
    """

    def __init__(self, stream: LineStream) -> None:
        """Bind the line stream without consuming it."""
        self._stream = stream
        self.header: StoreHeader | None = None
        self.had_torn_tail = False

    def __iter__(self) -> cabc.Iterator[DocumentRow]:
        """Iterate the header then typed rows in document order.

        Yields
        ------
        DocumentRow
            Header and record rows carrying their original bytes.

        Raises
        ------
        SegmentFaultError
            If the header, or a terminated row, is invalid.
        """
        iterator = iter(self._stream)
        first = next(iterator, None)
        if first is None:
            raise SegmentFaultError("corrupt")
        self.header = _decode_header(first)
        yield HeaderRow(first, self.header)
        pending: bytes | None = None
        for raw in iterator:
            if pending is not None:
                yield _decode_record(pending)
            pending = raw
        yield from self._finish(pending)

    def _finish(self, pending: bytes | None) -> cabc.Iterator[DocumentRow]:
        """Emit or drop the final row according to stream termination.

        Parameters
        ----------
        pending : bytes | None
            The final row, held back until its newline status is known.

        Yields
        ------
        DocumentRow
            The final row when it decodes and the stream is terminated.

        Raises
        ------
        SegmentFaultError
            If a terminated final row is invalid.
        """
        if pending is None:
            if not self._stream.is_terminated:
                self.had_torn_tail = True
            return
        decoded = outcome_record_codec.decode_record_line(pending)
        if isinstance(decoded, RecordFailure):
            if self._stream.is_terminated:
                raise SegmentFaultError("corrupt")
            self.had_torn_tail = True
            return
        yield RecordRow(pending, decoded)
        if not self._stream.is_terminated:
            self.had_torn_tail = True


def _decode_header(raw: bytes) -> StoreHeader:
    """Decode the first row as a supported header.

    Parameters
    ----------
    raw : bytes
        One raw header row.

    Returns
    -------
    StoreHeader
        The typed header.

    Raises
    ------
    SegmentFaultError
        If the row is not a supported header.
    """
    decoded = outcome_record_codec.decode_header_line(raw)
    if isinstance(decoded, RecordFailure):
        reason: FaultReason = (
            "version_mismatch" if decoded.reason == "version_mismatch" else "corrupt"
        )
        raise SegmentFaultError(reason)
    return decoded


def _decode_record(raw: bytes) -> RecordRow:
    """Decode one terminated row as a record.

    Parameters
    ----------
    raw : bytes
        One raw record row.

    Returns
    -------
    RecordRow
        The typed row with its original bytes.

    Raises
    ------
    SegmentFaultError
        If the row is not a valid record.
    """
    decoded = outcome_record_codec.decode_record_line(raw)
    if isinstance(decoded, RecordFailure):
        raise SegmentFaultError("corrupt")
    return RecordRow(raw, decoded)


def scan_archive(stream: LineStream) -> ArchiveFacts:
    """Read one archive or source to its end and prove its contents.

    Parameters
    ----------
    stream : LineStream
        A plain or XZ-compressed line stream.

    Returns
    -------
    ArchiveFacts
        The dialect, counts and digest the stream actually held.

    Raises
    ------
    SegmentFaultError
        If a row or header is invalid, or a compressed stream is incomplete.
    """
    reader = DocumentReader(stream)
    first: dt.datetime | None = None
    last: dt.datetime | None = None
    count = 0
    try:
        for row in reader:
            if isinstance(row, RecordRow):
                count += 1
                observed = row.record.observed_at
                first = observed if first is None or observed < first else first
                last = observed if last is None or observed > last else last
    except StreamFaultError as fault:
        raise SegmentFaultError(fault.reason) from fault
    if reader.header is None or not stream.is_verified:
        raise SegmentFaultError("truncated_xz")
    return ArchiveFacts(
        salt_id=reader.header.salt_id,
        record_count=count,
        logical_bytes=stream.bytes_read,
        sha256=stream.sha256_hexdigest,
        first_observed_at=first,
        last_observed_at=last,
        had_torn_tail=reader.had_torn_tail,
    )


def write_archive(
    stream: LineStream,
    sink: typ.BinaryIO,
    *,
    preset: int,
    trim: Trim,
) -> ArchiveFacts:
    """Compress one document into ``sink`` while applying a prefix trim.

    Parameters
    ----------
    stream : LineStream
        The uncompressed source rows.
    sink : BinaryIO
        The temporary archive destination.
    preset : int
        XZ preset from 0 to 9.
    trim : Trim
        The prefix policy; an empty trim keeps every row.

    Returns
    -------
    ArchiveFacts
        Facts describing the bytes actually written.

    Raises
    ------
    SegmentFaultError
        If the source header or any kept row is invalid.

    Examples
    --------
    >>> import datetime as dt, io
    >>> header = StoreHeader("s", dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    >>> text = outcome_record_codec.encode_line(header) + chr(10)
    >>> source = LineStream(io.BytesIO(text.encode()))
    >>> facts = write_archive(source, io.BytesIO(), preset=0, trim=Trim())
    >>> facts.record_count, facts.salt_id
    (0, 's')
    """
    reader = DocumentReader(stream)
    compressor = lzma.LZMACompressor(
        format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, preset=preset
    )
    emitted = _Emitted(sink, compressor)
    seen = 0
    consumed = 0
    first: dt.datetime | None = None
    last: dt.datetime | None = None
    try:
        for row in reader:
            consumed += len(row.raw) + 1
            if isinstance(row, HeaderRow):
                emitted.push_header(row.raw)
                continue
            keep = _should_keep(row, trim, seen=seen, consumed=consumed)
            seen += 1
            if not keep:
                continue
            emitted.push_record(row.raw)
            observed = row.record.observed_at
            first = observed if first is None or observed < first else first
            last = observed if last is None or observed > last else last
    except StreamFaultError as fault:
        raise SegmentFaultError(fault.reason) from fault
    if reader.header is None:
        raise SegmentFaultError("corrupt")
    emitted.finish()
    return ArchiveFacts(
        salt_id=reader.header.salt_id,
        record_count=emitted.records,
        logical_bytes=emitted.logical_bytes,
        sha256=emitted.sha256,
        first_observed_at=first,
        last_observed_at=last,
        had_torn_tail=reader.had_torn_tail,
    )


def _should_keep(row: RecordRow, trim: Trim, *, seen: int, consumed: int) -> bool:
    """Decide whether one record survives the prefix trim.

    Parameters
    ----------
    row : RecordRow
        The candidate record.
    trim : Trim
        The prefix policy.
    seen : int
        Records already decided, used by the explicit count trim.
    consumed : int
        Source bytes represented by this row and everything before it.

    Returns
    -------
    bool
        Whether the row is retained.
    """
    if seen < trim.records:
        return False
    if trim.before is not None and row.record.observed_at < trim.before:
        return False
    if trim.allowed_bytes is None:
        return True
    return trim.source_bytes - consumed <= trim.allowed_bytes


class _Emitted:
    """Accumulate the archive's own counts and digest as bytes are written."""

    def __init__(self, sink: typ.BinaryIO, compressor: lzma.LZMACompressor) -> None:
        """Bind the sink and compressor and clear the counters."""
        self._sink = sink
        self._compressor = compressor
        self._hasher = hashlib.sha256()
        self.logical_bytes = 0
        self.records = 0

    def push_header(self, raw: bytes) -> None:
        """Write the one header row without counting it as a record."""
        self._write(raw)

    def push_record(self, raw: bytes) -> None:
        """Write one retained record row and count it."""
        self._write(raw)
        self.records += 1

    def finish(self) -> None:
        """Flush the XZ footer so the archive is a complete stream."""
        self._sink.write(self._compressor.flush())

    def _write(self, raw: bytes) -> None:
        """Compress and count one newline-terminated row."""
        data = raw + b"\n"
        self._sink.write(self._compressor.compress(data))
        self._hasher.update(data)
        self.logical_bytes += len(data)

    @property
    def sha256(self) -> str:
        """Digest of the emitted content.

        Returns
        -------
        str
            A lowercase SHA-256 digest.
        """
        return self._hasher.hexdigest()
