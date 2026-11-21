"""Read the whole retained outcome history across every segment state.

Archived XZ segments, sealed plain segments and the writer's active file are
read in append order. ``read_history`` returns a ``DecodedStore``, which by
definition holds every selected record; the dataset snapshot needs that, so
its memory follows the selected domain rather than the largest segment.

A caller that only visits the rows should stream them instead.
``outcome_history_stream.open_history`` pins one consistent snapshot and
yields its rows one segment at a time, filterable by salt domain, so replay
holds at most one segment's decoder state plus the active file's bytes. Both
readers share the same settling rule: the scan, the read and a second scan
must agree, and a store the writer keeps changing fails closed.

A salt change partitions history: the default reads the newest salt domain,
an explicit identifier reads that domain, and ``--all-salts`` reads every
domain in append order without implying that their records are comparable.
"""

import typing as typ

from buildlimits.outcome_history_pin import (
    HISTORY_ATTEMPTS,
    HistoryFaultError,
    HistorySelection,
)
from buildlimits.outcome_history_stream import (
    HistoryStream,
    open_history,
    open_history_beside,
)
from buildlimits.outcome_record_format import DecodedStore
from buildlimits.outcome_segments import SegmentLayout

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = [
    "HISTORY_ATTEMPTS",
    "HistoryFaultError",
    "HistorySelection",
    "HistoryStream",
    "open_history",
    "open_history_beside",
    "read_history",
    "read_history_beside",
]


def read_history(
    layout: SegmentLayout,
    selection: HistorySelection | None = None,
    *,
    on_retry: cabc.Callable[[str, int, str], None] | None = None,
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
    on_retry : Callable[[str, int, str], None] | None
        Receives each retried pin's operation, attempt number and category.

    Returns
    -------
    DecodedStore
        The chosen header and every matching record in append order.

    Raises
    ------
    HistoryFaultError
        If the store never settles, a segment is corrupt, or the named salt
        was never written.

    Notes
    -----
    A store with no segment and no active file raises ``FileNotFoundError``.

    Examples
    --------
    ``read_history(layout).records`` holds archived and active rows in the
    order the writer produced them.
    """
    chosen = HistorySelection() if selection is None else selection
    last: HistoryFaultError | None = None
    for attempt in range(HISTORY_ATTEMPTS):
        stream = open_history(layout, chosen, on_retry=on_retry)
        try:
            records = tuple(stream)
        except HistoryFaultError as error:
            if error.reason != "unsettled":
                raise
            last = error
            if on_retry is not None:
                on_retry("history", attempt + 1, "unsettled")
            continue
        salts = stream.salt_ids if chosen.all_salts else ()
        return DecodedStore(stream.header, records, salts)
    if last is not None:
        raise last
    raise HistoryFaultError("unsettled")


def read_history_beside(
    store: Path,
    selection: HistorySelection | None = None,
    *,
    on_retry: cabc.Callable[[str, int, str], None] | None = None,
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
    on_retry : Callable[[str, int, str], None] | None
        Receives each retried pin's operation, attempt number and category.

    Returns
    -------
    DecodedStore
        The chosen header and every matching record in append order.

    Examples
    --------
    ``read_history_beside(store, HistorySelection(all_salts=True))`` reads
    every retained domain, whichever salt is now active.
    """
    return read_history(SegmentLayout.beside(store), selection, on_retry=on_retry)
