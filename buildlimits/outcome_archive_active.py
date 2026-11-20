"""Bounded, read-only measurement of the active file for diagnostics.

Retention decisions never need the active file's rows: its size from ``lstat``
is the whole contribution to the budget. Reporting what that budget actually
retains does need a row count and an observation span, so this adapter reads
the active file once through the same bounded reader the writer uses. It only
reads: the writer remains the only component that appends, truncates or
replaces the file.

The read is bounded by the store's maximum size, so a file that somehow grew
past it reports an incomplete lower bound rather than allocating the whole
document. A concurrent append can only add a complete final line or a tail the
reader discards, so the snapshot is either exact or one row short.
"""

import typing as typ

from buildlimits import outcome_records
from buildlimits.outcome_retention_report import ActiveHistory, measured_history
from buildlimits.outcome_store_io import read_store, store_size

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = ["read_active_history"]


def read_active_history(path: Path) -> ActiveHistory:
    """Count the rows and observation span of the active file.

    An ``OSError`` from measuring or reading an existing file propagates, so a
    caller reports an unreadable store rather than an empty one.

    Parameters
    ----------
    path : Path
        The active cache-outcome store.

    Returns
    -------
    ActiveHistory
        Counts, the file's logical size and whether every row was counted.

    Examples
    --------
    >>> from pathlib import Path
    >>> read_active_history(Path("no-such-store.jsonl")).record_count
    0
    """
    size = store_size(path)
    try:
        decoded = read_store(path)
    except FileNotFoundError:
        return ActiveHistory()
    match decoded:
        case outcome_records.DecodedStore() as store:
            return measured_history(store.records, logical_bytes=size)
        case outcome_records.RecordFailure() as failure:
            # The size is still a fact, but no row or time can be trusted.
            return ActiveHistory(
                logical_bytes=size, complete=False, reason=failure.reason
            )
        case _:
            return ActiveHistory(logical_bytes=size, complete=False, reason="corrupt")
