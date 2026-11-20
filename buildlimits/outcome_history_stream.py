"""A re-iterable, salt-filtered stream over the retained outcome history.

A dataset snapshot holds every selected row by definition, but replay only
visits them, twice at most, and must not hold the whole retained history in
memory. ``HistoryStream`` therefore wraps one snapshot pinned by
``outcome_history_pin.pin_history`` -- a device, inode and size fingerprint
per closed segment, plus the active file's bytes -- and each iteration
re-streams that snapshot one segment at a time, decoding rows lazily and
skipping a domain the caller did not select.

A pass that finds a pinned segment changed fails closed as ``unsettled``:
the pre-pass check covers every segment, and each opened descriptor is
checked again, so a replacement between the check and the open is refused
rather than read. The caller may open a fresh stream. Because every pass
reads the same frozen snapshot, a two-pass comparison such as H2 compares
the same rows. Callers that need the whole history in memory call
``outcome_history.read_history``, which collects this stream.
"""

import dataclasses as dc
import io
import os
import typing as typ

from buildlimits.outcome_archive_rows import (
    DocumentReader,
    HeaderRow,
    SegmentFaultError,
)
from buildlimits.outcome_history_pin import (
    HistoryFaultError,
    HistorySelection,
    _PinnedActive,
    _PinnedSegment,
    _Snapshot,
    pin_history,
)
from buildlimits.outcome_history_settle import fingerprint
from buildlimits.outcome_record_stream import LineStream, StreamFaultError
from buildlimits.outcome_segments import SegmentLayout

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreHeader, StoreRecord

__all__ = [
    "HistoryStream",
    "open_history",
    "open_history_beside",
]


@dc.dataclass(frozen=True, slots=True)
class HistoryStream:
    """A re-iterable snapshot of the retained history.

    Attributes
    ----------
    header : StoreHeader
        The newest header the selection covers.
    salt_ids : tuple[str, ...]
        Every domain the snapshot carries, oldest first.
    """

    snapshot: _Snapshot

    @property
    def header(self) -> StoreHeader:
        """The newest header the selection covers.

        Returns
        -------
        StoreHeader
            The header that describes the selected domain.
        """
        return self.snapshot.header

    @property
    def salt_ids(self) -> tuple[str, ...]:
        """Every record domain the snapshot carries, oldest first.

        Returns
        -------
        tuple[str, ...]
            Distinct salt identifiers in append order.
        """
        return self.snapshot.salt_ids

    def __iter__(self) -> cabc.Iterator[StoreRecord]:
        """Yield the selected snapshot's records in append order.

        Each iteration decodes one segment at a time, so a pass holds at most
        one segment's decoder state rather than every retained row. A segment
        that changed after the pin, or a row that does not decode, raises
        ``HistoryFaultError`` rather than shortening the pass.

        Yields
        ------
        StoreRecord
            The selected domain's rows, oldest first.
        """
        _require_unchanged(self.snapshot.segments)
        for segment in self.snapshot.segments:
            if self._selects(segment.header.salt_id):
                yield from _iter_segment(segment)
        active = self.snapshot.active
        if active is not None and self._selects(active.header.salt_id):
            yield from _iter_active(active)

    def _selects(self, salt_id: str) -> bool:
        """Whether one document's domain belongs to this snapshot's selection.

        Returns
        -------
        bool
            True for every domain when the selection is unrestricted.
        """
        selected = self.snapshot.selected
        return selected is None or selected == salt_id


def open_history(
    layout: SegmentLayout, selection: HistorySelection | None = None
) -> HistoryStream:
    """Pin one consistent snapshot of the retained history.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    selection : HistorySelection | None
        The record domain to read; the newest domain when None.

    Returns
    -------
    HistoryStream
        A re-iterable view of the snapshot.

    Notes
    -----
    The pin reports a store that never settles, a vanished segment, a corrupt
    segment or an unwritten salt as ``HistoryFaultError``, and a store with no
    segment and no active file as ``FileNotFoundError``.
    """
    return HistoryStream(pin_history(layout, selection))


def open_history_beside(
    store: Path, selection: HistorySelection | None = None
) -> HistoryStream:
    """Pin the retained history that belongs to one store path.

    Parameters
    ----------
    store : Path
        The configured ``cache-outcomes.jsonl`` path.
    selection : HistorySelection | None
        The record domain to read; the newest domain when None.

    Returns
    -------
    HistoryStream
        A re-iterable view of the snapshot.

    Examples
    --------
    ``open_history_beside(store, HistorySelection(all_salts=True))`` streams
    every retained domain, whichever salt is now active.
    """
    return open_history(SegmentLayout.beside(store), selection)


def _require_unchanged(segments: tuple[_PinnedSegment, ...]) -> None:
    """Refuse a pass whose pinned segments are no longer the files read.

    Raises
    ------
    HistoryFaultError
        If a pinned segment was deleted or replaced after the pin.
    """
    for segment in segments:
        try:
            current = fingerprint(segment.path)
        except FileNotFoundError as error:
            raise HistoryFaultError("unsettled") from error
        if current != segment.fingerprint:
            raise HistoryFaultError("unsettled")


def _iter_segment(segment: _PinnedSegment) -> cabc.Iterator[StoreRecord]:
    """Yield one pinned segment's records, decoding as the caller reads.

    Yields
    ------
    StoreRecord
        The segment's rows in append order.

    Raises
    ------
    HistoryFaultError
        If the file vanished or a row is invalid.
    """
    try:
        with segment.path.open("rb") as source:
            _require_same_file(source, segment)
            stream = LineStream(source, is_compressed=segment.is_compressed)
            yield from _records(stream)
    except FileNotFoundError as error:
        raise HistoryFaultError("unsettled") from error


def _require_same_file(source: typ.BinaryIO, segment: _PinnedSegment) -> None:
    """Refuse a descriptor that is not the file the pin fingerprinted.

    The pre-pass check cannot close the window between itself and this open:
    retention may replace a later segment in between. Comparing the opened
    descriptor's identity is the check that binds the rows to the snapshot.

    Raises
    ------
    HistoryFaultError
        If the opened file is not the pinned inode and size.
    """
    info = os.fstat(source.fileno())
    if (info.st_dev, info.st_ino, info.st_size) != segment.fingerprint:
        raise HistoryFaultError("unsettled")


def _iter_active(active: _PinnedActive) -> cabc.Iterator[StoreRecord]:
    """Yield the frozen active file's committed records.

    The writer commits an append by writing its newline, and it removes any
    unterminated remainder before its next append, so a final row without one
    is the append in flight. ``read_store`` decodes only up to the last
    newline, and this reader holds the final row back to match: yielding it
    would hand replay and dataset snapshots a row the ordinary reader never
    returns and the writer may still roll back. A closed segment keeps its
    own rule, because a complete row whose newline never reached the disk is
    history that must not be lost there.

    Yields
    ------
    StoreRecord
        The file's committed rows in append order.

    Raises
    ------
    HistoryFaultError
        If the header or a terminated row does not decode.
    """
    reader = DocumentReader(LineStream(io.BytesIO(active.data)))
    pending: StoreRecord | None = None
    try:
        for row in reader:
            if isinstance(row, HeaderRow):
                continue
            if pending is not None:
                yield pending
            pending = row.record
    except (SegmentFaultError, StreamFaultError) as error:
        raise HistoryFaultError(str(error.reason)) from error
    if pending is not None and reader.last_record_terminated:
        yield pending


def _records(stream: LineStream) -> cabc.Iterator[StoreRecord]:
    """Decode one line stream into its records, dropping the header.

    Yields
    ------
    StoreRecord
        Each decoded record row.

    Raises
    ------
    HistoryFaultError
        If the stream or a row is invalid.
    """
    try:
        for row in DocumentReader(stream):
            if not isinstance(row, HeaderRow):
                yield row.record
    except (SegmentFaultError, StreamFaultError) as error:
        raise HistoryFaultError(str(error.reason)) from error
