"""Bounded, read-only measurement of the active file for retention and reports.

The active file's size from ``lstat`` is the whole contribution to the byte
budget, while the record cap also needs its row count. Reporting what the
store retains needs that count and an observation span as well, so this
adapter reads the active file once through the same bounded reader the writer
uses. It only reads: the writer remains the only component that appends,
truncates or replaces the file.

One read supplies both the byte length and the rows, so an append that races
the read cannot put the new row count beside the old file size. The read is
bounded by the store's maximum size, so a file that somehow grew past it
reports an incomplete lower bound rather than allocating the whole document.
"""

import typing as typ

from buildlimits import outcome_records
from buildlimits.outcome_retention_report import ActiveHistory, measured_history
from buildlimits.outcome_store_read import read_store_snapshot

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = ["active_identity", "read_active_history"]


def active_identity(path: Path) -> tuple[int, int] | None:
    """Return the identity a writer changes when it replaces the active file.

    A seal or a compaction installs a new file, so the device and inode pair
    changes; an append does not. Comparing the pair either side of a closed
    history read tells a caller whether both reads describe the same active
    file, which is what stops a sealed segment being counted twice.

    Parameters
    ----------
    path : Path
        The active cache-outcome store.

    Returns
    -------
    tuple[int, int] | None
        The file's ``(st_dev, st_ino)`` pair, or None while no file exists.

    Examples
    --------
    >>> from pathlib import Path
    >>> active_identity(Path("no-such-store.jsonl")) is None
    True
    """
    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    return (status.st_dev, status.st_ino)


def read_active_history(path: Path) -> ActiveHistory:
    """Count the rows and observation span of the active file.

    An ``OSError`` from reading an existing file propagates, so a caller
    reports an unreadable store rather than an empty one.

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
    try:
        snapshot = read_store_snapshot(path)
    except FileNotFoundError:
        return ActiveHistory()
    match snapshot.decoded:
        case outcome_records.DecodedStore() as store:
            return measured_history(store.records, logical_bytes=snapshot.logical_bytes)
        case outcome_records.RecordFailure() as failure:
            # The size is still a fact, but no row or time can be trusted.
            return ActiveHistory(
                logical_bytes=snapshot.logical_bytes,
                complete=False,
                reason=failure.reason,
            )
        case _:
            return ActiveHistory(
                logical_bytes=snapshot.logical_bytes,
                complete=False,
                reason="corrupt",
            )
