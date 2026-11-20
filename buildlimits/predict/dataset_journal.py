"""The dataset command's journal options and provenance note.

The snapshot is built from the outcome store, so the user journal is context:
a journal that cannot answer becomes an explicit unavailable note rather than
failing the command. This module is the one place the command's journal
options meet the journal reader.
"""

import argparse
import typing as typ

from buildlimits.metrics_query import MAX_RECORDS, Window, window_seconds
from buildlimits.metrics_query import query as read_journal
from buildlimits.predict.journal_provenance import (
    JournalProvenance,
    JournalUnavailable,
    count_journal,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["journal_note", "journal_span", "journal_window"]


def journal_span(text: str) -> str:
    """Return a journal lookback once it parses as a positive duration.

    The provenance note is best effort, so an unparsable span would otherwise
    be swallowed by the note's own failure handling: the command would write a
    snapshot with a generic ``journal query failed`` note and still exit 0,
    leaving the usage error invisible. Validating at parse time reports it the
    way every other bad option is reported.

    Returns
    -------
    str
        The span, unchanged.

    Raises
    ------
    argparse.ArgumentTypeError
        When the span is not a positive duration such as ``30m`` or ``1h``.
    """
    try:
        window_seconds(text)
    except ValueError as error:
        msg = "must be a positive duration such as 30m or 1h"
        raise argparse.ArgumentTypeError(msg) from error
    return text


def journal_window(since: str) -> Window:
    """Ask the user journal for its most recent records.

    Returns
    -------
    Window
        The records the journal served within the cap, and whether it hit it.
    """
    return read_journal(since=since, max_records=MAX_RECORDS, source="all")


def journal_note(journal: cabc.Callable[[str], Window], span: str) -> JournalProvenance:
    """Ask the journal what it can offer, tolerating any failure.

    The reader reports its own failure rather than returning a placeholder, so
    the conversion to an unavailable note happens here: the snapshot comes
    from the store and must not be lost to a side query.

    Returns
    -------
    JournalProvenance
        The journal summary, or an explicit unavailable note.
    """
    try:
        window = journal(span)
    except Exception:  # ruff: ignore[blind-except] - provenance must never fail the snapshot.
        return JournalUnavailable("journal query failed")
    return count_journal(
        window.records, since=span, cap=MAX_RECORDS, truncated=window.truncated
    )
