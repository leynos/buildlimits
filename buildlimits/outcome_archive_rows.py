"""Decode one segment's rows into typed values, tolerating a torn tail.

A segment is a header row followed by record rows. An unterminated final
fragment is the one defect a live writer can leave, so it is dropped and
flagged; anything else that does not decode is a closed fault the caller
quarantines.
"""

import dataclasses as dc
import typing as typ

from buildlimits import outcome_record_codec
from buildlimits.outcome_record_format import RecordFailure, StoreHeader, StoreRecord
from buildlimits.outcome_record_stream import StreamFaultError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_record_stream import LineStream

type FaultReason = typ.Literal[
    "corrupt",
    "version_mismatch",
    "truncated_xz",
    "unverified_plain",
    "trailing_data",
    "bad_xz",
    "line_too_long",
]

__all__ = [
    "DocumentReader",
    "DocumentRow",
    "FaultReason",
    "HeaderRow",
    "RecordRow",
    "SegmentFaultError",
    "iter_records",
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
        Whether the stream ended without a newline, so its final fragment is
        not committed history.
    last_record_terminated : bool
        Whether the last record row yielded ended with a newline. A dropped
        undecodable fragment leaves this true: the row before it was committed,
        and only the fragment was torn.
    dropped_tail_bytes : int
        Bytes of an undecodable unterminated fragment this reader dropped. A
        decodable unterminated row is yielded, not dropped, so it is not
        counted: a writer that terminates it keeps those bytes.
    """

    def __init__(self, stream: LineStream) -> None:
        """Bind the line stream without consuming it."""
        self._stream = stream
        self.header: StoreHeader | None = None
        self.had_torn_tail = False
        self.last_record_terminated = True
        self.dropped_tail_bytes = 0

    def __iter__(self) -> cabc.Iterator[DocumentRow]:
        """Iterate the header then typed rows in document order.

        Each row is decoded as the version the document's header names. The
        accepted row shape differs by version, so a current document's rows must
        state their provenance while an older document's rows may predate it.
        Decoding every row as the current version would reject readable history
        and would accept a current row that omits its marker.

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
        version = self.header.version
        yield HeaderRow(first, self.header)
        pending: bytes | None = None
        for raw in iterator:
            if pending is not None:
                yield _decode_record(pending, version)
            pending = raw
        yield from self._finish(pending, version)

    def _finish(
        self, pending: bytes | None, version: int
    ) -> cabc.Iterator[DocumentRow]:
        """Emit or drop the final row according to stream termination.

        An unterminated final row that decodes is still yielded, because its
        bytes are readable history; whether it was committed is reported
        separately through ``last_record_terminated``. An unterminated final
        fragment that does not decode is dropped as the torn tail it is, and
        the row before it keeps its termination status: only the fragment was
        torn.

        Parameters
        ----------
        pending : bytes | None
            The final row, held back until its newline status is known.
        version : int
            The version of the document the row belongs to.

        Yields
        ------
        DocumentRow
            The final row when it decodes.

        Raises
        ------
        SegmentFaultError
            If a terminated final row is invalid.
        """
        if pending is None:
            if not self._stream.is_terminated:
                self.had_torn_tail = True
            return
        decoded = outcome_record_codec.decode_record_line(pending, version=version)
        if isinstance(decoded, RecordFailure):
            if self._stream.is_terminated:
                raise SegmentFaultError("corrupt")
            self.had_torn_tail = True
            self.dropped_tail_bytes = len(pending)
            return
        yield RecordRow(pending, decoded)
        if not self._stream.is_terminated:
            self.had_torn_tail = True
            self.last_record_terminated = False


def iter_records(reader: DocumentReader) -> cabc.Iterator[RecordRow]:
    """Yield each record row, translating a stream fault into a segment fault.

    Every consumer of a document walks it the same way: record rows are what it
    acts on, and a stream failure is the segment fault the caller reports or
    quarantines. Sharing that walk keeps the translation in one place. The
    caller keeps the reader, so it can still settle the facts that only exist
    after the last row, such as the header and a dropped torn tail.

    Parameters
    ----------
    reader : DocumentReader
        The reader over one line stream.

    Yields
    ------
    RecordRow
        Each decoded record row in document order.

    Raises
    ------
    SegmentFaultError
        If a row or header is invalid, or a compressed stream is incomplete.
    """
    try:
        for row in reader:
            if isinstance(row, RecordRow):
                yield row
    except StreamFaultError as fault:
        raise SegmentFaultError(fault.reason) from fault


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


def _decode_record(raw: bytes, version: int) -> RecordRow:
    """Decode one terminated row as a record.

    Parameters
    ----------
    raw : bytes
        One raw record row.
    version : int
        The version of the document the row belongs to.

    Returns
    -------
    RecordRow
        The typed row with its original bytes.

    Raises
    ------
    SegmentFaultError
        If the row is not a valid record.
    """
    decoded = outcome_record_codec.decode_record_line(raw, version=version)
    if isinstance(decoded, RecordFailure):
        raise SegmentFaultError("corrupt")
    return RecordRow(raw, decoded)
