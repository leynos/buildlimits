"""Read the whole retained outcome history across every segment state.

Archived XZ segments, sealed plain segments and the writer's active file are
read in append order and decoded into one record stream. Each segment is
streamed and verified in turn, so memory follows one segment rather than the
whole retained history. A salt change partitions history: the default reads
the newest salt domain, an explicit identifier reads that domain, and
``--all-salts`` reads every domain in append order without implying that their
records are comparable.
"""

import dataclasses as dc
import typing as typ

from buildlimits import outcome_segment_inventory
from buildlimits.outcome_archive_verify import (
    DocumentReader,
    HeaderRow,
    RecordRow,
    SegmentFaultError,
)
from buildlimits.outcome_record_format import DecodedStore
from buildlimits.outcome_record_stream import LineStream, StreamFaultError
from buildlimits.outcome_segments import SegmentLayout, archive_name

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreHeader, StoreRecord

__all__ = [
    "HistoryFaultError",
    "HistorySelection",
    "read_history",
    "read_history_beside",
]


class HistoryFaultError(Exception):
    """A closed reason the retained history cannot be read.

    Parameters
    ----------
    reason : str
        The fault classification, never raw exception text.
    """

    def __init__(self, reason: str) -> None:
        """Retain the closed reason as the exception message."""
        super().__init__(reason)
        self.reason = reason


@dc.dataclass(frozen=True, slots=True)
class HistorySelection:
    """Which record domain a caller wants to read.

    Attributes
    ----------
    salt_id : str | None
        Read this salt domain, or the newest domain when None.
    all_salts : bool
        Read every domain in append order, ignoring ``salt_id``.
    """

    salt_id: str | None = None
    all_salts: bool = False


@dc.dataclass(frozen=True, slots=True)
class _Document:
    """One segment's header and decoded rows."""

    header: StoreHeader
    records: tuple[StoreRecord, ...]


def read_history(
    layout: SegmentLayout,
    selection: HistorySelection | None = None,
) -> DecodedStore:
    """Read retained history across archived, sealed and active segments.

    A corrupt segment surfaces as ``HistoryFaultError`` from the decode
    helpers, as does an explicitly named salt that no retained segment
    carries.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    selection : HistorySelection | None
        The record domain to read; the newest domain when None.

    Returns
    -------
    DecodedStore
        The chosen header and every matching record in append order.

    Raises
    ------
    FileNotFoundError
        If no segment or active file exists.

    Examples
    --------
    ``read_history(layout).records`` holds archived and active rows in the
    order the writer produced them.
    """
    chosen = HistorySelection() if selection is None else selection
    documents = tuple(_documents(layout))
    if not documents:
        raise FileNotFoundError(layout.active)
    newest = documents[-1].header
    if chosen.all_salts:
        return DecodedStore(newest, _records(documents))
    if chosen.salt_id is not None:
        return _one_salt(documents, chosen.salt_id)
    return DecodedStore(newest, _same_salt(documents, newest.salt_id))


def read_history_beside(
    store: Path,
    selection: HistorySelection | None = None,
) -> DecodedStore:
    """Read the retained history that belongs to one store path.

    The store path names the active file; archived and sealed segments are
    found in the sibling directories a writer and archiver use.

    Parameters
    ----------
    store : Path
        The configured ``cache-outcomes.jsonl`` path.
    selection : HistorySelection | None
        The record domain to read; the newest domain when None.

    Returns
    -------
    DecodedStore
        The chosen header and every matching record in append order.

    Examples
    --------
    ``read_history_beside(store, HistorySelection(all_salts=True))`` reads
    every retained domain, whichever salt is now active.
    """
    return read_history(SegmentLayout.beside(store), selection)


def _one_salt(documents: tuple[_Document, ...], salt_id: str) -> DecodedStore:
    """Read one explicitly named salt domain.

    Returns
    -------
    DecodedStore
        The domain's newest header and every matching record.

    Raises
    ------
    HistoryFaultError
        If no retained segment carries the requested salt.
    """
    matching = [
        document for document in documents if document.header.salt_id == salt_id
    ]
    if not matching:
        raise HistoryFaultError("unknown_salt")
    return DecodedStore(matching[-1].header, _records(matching))


def _same_salt(
    documents: tuple[_Document, ...], salt_id: str
) -> tuple[StoreRecord, ...]:
    """Collect records belonging to one salt domain.

    Returns
    -------
    tuple[StoreRecord, ...]
        Matching records in append order.
    """
    return _records([
        document for document in documents if document.header.salt_id == salt_id
    ])


def _records(documents: cabc.Iterable[_Document]) -> tuple[StoreRecord, ...]:
    """Flatten the records of several segments in append order.

    Returns
    -------
    tuple[StoreRecord, ...]
        Every record from the supplied documents.
    """
    return tuple(record for document in documents for record in document.records)


def _documents(layout: SegmentLayout) -> cabc.Iterator[_Document]:
    """Iterate one decoded document per retained segment, oldest first.

    A corrupt or truncated segment surfaces as ``HistoryFaultError`` from the
    per-document decoder.

    Yields
    ------
    _Document
        The segment's header and records.

    Raises
    ------
    FileNotFoundError
        When an entry the scan reported as archived has since disappeared.
        A sealed source is different: its archive stands in for the removed
        name, so only an already-archived path is re-raised.
    """
    inventory = outcome_segment_inventory.read_inventory(layout)
    for entry in inventory.effective:
        try:
            yield _read_document(entry.path, is_compressed=entry.state == "archived")
        except FileNotFoundError:
            # The archiver may have removed this sealed source after
            # publishing its archive, which the scan did not list yet. Read
            # the archive it was replaced by so the segment's rows survive;
            # skipping the vanished name would silently shorten history.
            if entry.state == "archived":
                raise
            yield _read_document(
                layout.archive_dir / archive_name(entry.sequence),
                is_compressed=True,
            )
    if layout.active.exists():
        yield _read_document(layout.active, is_compressed=False)


def _read_document(path: Path, *, is_compressed: bool) -> _Document:
    """Decode one plain or XZ-compressed segment to its end.

    Parameters
    ----------
    path : Path
        The segment file.
    is_compressed : bool
        Whether the file holds an XZ stream.

    Returns
    -------
    _Document
        The segment's header and records.

    Raises
    ------
    HistoryFaultError
        If the stream or a row is invalid.
    """
    header: StoreHeader | None = None
    records: list[StoreRecord] = []
    try:
        with path.open("rb") as source:
            rows = DocumentReader(LineStream(source, is_compressed=is_compressed))
            for row in rows:
                match row:
                    case HeaderRow(header=first) if header is None:
                        header = first
                    case RecordRow(record=record):
                        records.append(record)
    except (SegmentFaultError, StreamFaultError) as error:
        raise HistoryFaultError(str(error.reason)) from error
    if header is None:
        raise HistoryFaultError("corrupt")
    return _Document(header, tuple(records))
