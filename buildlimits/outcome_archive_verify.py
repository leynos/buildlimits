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

from buildlimits.outcome_archive_rows import (
    DocumentReader,
    HeaderRow,
    RecordRow,
    SegmentFaultError,
)
from buildlimits.outcome_record_stream import LineStream, StreamFaultError

if typ.TYPE_CHECKING:
    import datetime as dt

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
    >>> text = outcome_record_encode.encode_line(header) + chr(10)
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
                # The header belongs in the archive but is not a record.
                emitted.write(row.raw)
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
    # ``consumed`` already includes this row, so add its bytes back: the
    # suffix that survives is this row plus everything after it, and that
    # whole suffix must fit the allowance. ``_planned_size`` relies on this
    # bound, so comparing only the later bytes would overstate freed space by
    # one row.
    retained = trim.source_bytes - consumed + len(row.raw) + 1
    return retained <= trim.allowed_bytes


class _Emitted:
    """Accumulate the archive's own counts and digest as bytes are written."""

    def __init__(self, sink: typ.BinaryIO, compressor: lzma.LZMACompressor) -> None:
        """Bind the sink and compressor and clear the counters."""
        self._sink = sink
        self._compressor = compressor
        self._hasher = hashlib.sha256()
        self.logical_bytes = 0
        self.records = 0

    def push_record(self, raw: bytes) -> None:
        """Write one retained record row and count it."""
        self.write(raw)
        self.records += 1

    def finish(self) -> None:
        """Flush the XZ footer so the archive is a complete stream."""
        self._sink.write(self._compressor.flush())

    def write(self, raw: bytes) -> None:
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
