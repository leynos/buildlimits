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
    iter_records,
)
from buildlimits.outcome_archive_trim import Trim, TrimWalk
from buildlimits.outcome_record_stream import StreamFaultError

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_record_stream import LineStream

__all__ = [
    "ArchiveFacts",
    "DocumentReader",
    "HeaderRow",
    "OutputFaultError",
    "RecordRow",
    "SegmentFaultError",
    "scan_archive",
    "write_archive",
]


class OutputFaultError(SegmentFaultError):
    """The archiver's own output failed verification.

    A source that decodes cleanly but whose archive does not decompress to the
    bytes written names a fault in the temporary file or the compressor, not a
    defect in the source. The closed reason is ``truncated_xz`` for the report,
    but the separate type lets the runner refuse to quarantine a source that is
    still perfectly readable.
    """


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
    for row in iter_records(reader):
        count += 1
        observed = row.record.observed_at
        first = observed if first is None or observed < first else first
        last = observed if last is None or observed > last else last
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
        The prefix policy; an empty trim keeps every row. A byte allowance
        that composes with a count or age rule is exact only when the trim
        carries the ``policy_bytes`` that ``policy_dropped_bytes`` measured
        for this source.

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
    >>> from buildlimits.outcome_record_encode import encode_line
    >>> from buildlimits.outcome_record_format import StoreHeader
    >>> from buildlimits.outcome_record_stream import LineStream
    >>> header = StoreHeader("s", dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    >>> text = encode_line(header) + chr(10)
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
    walk = TrimWalk(trim)
    first: dt.datetime | None = None
    last: dt.datetime | None = None
    try:
        for row in reader:
            if isinstance(row, HeaderRow):
                # The header belongs in the archive but is not a record.
                emitted.write(row.raw)
                continue
            if not walk.keep(row.raw, row.record.observed_at):
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
