"""Coherent status snapshots of the writer's active file.

The worker publishes the rows and byte size of each completed store state,
so a diagnostic read never pairs one state's rows with another state's size.
An append that races the read is either wholly included in a later snapshot
or left to the next one.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_archive_history import ActiveHistory, measured_history
from buildlimits.outcome_store_io import store_size_if_present

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_store_state import StoreFiles

__all__ = ["publish_snapshot", "retention_snapshot"]


@dc.dataclass(frozen=True, slots=True)
class _Published:
    """One completed store state, as the writer's worker observed it.

    Attributes
    ----------
    rows : list[StoreRecord]
        The row list that state held. The worker only appends to a list it has
        published before replacing it with a new one, so a reader can take the
        published prefix while the worker appends.
    count : int
        The number of rows the state held.
    logical_bytes : int | None
        The active file's size in that state, or None when no file exists or
        it could not be measured.
    reason : str | None
        A closed reason the size and the count do not describe one state, or
        None when they do.
    """

    rows: list[StoreRecord]
    count: int
    logical_bytes: int | None
    reason: str | None = None


def publish_snapshot(files: StoreFiles) -> None:
    """Publish the rows and byte size of one completed store state.

    The worker owns every mutation and calls this after each transition it
    completes, so one immutable pairing always describes one moment. A status
    read can then report a coherent view without opening the file, waiting for
    an append or seeing a row that reached disk but not the writer's rows.

    Parameters
    ----------
    files : StoreFiles
        The writer state whose completed transition is published.
    """
    try:
        size: int | None = store_size_if_present(files.path)
    except OSError:
        size = None
    reason = "repair_pending" if files.torn_tail else None
    files.publish_snapshot_state(
        _Published(files.records, len(files.records), size, reason)
    )


def retention_snapshot(files: StoreFiles) -> ActiveHistory:
    """Describe the rows the writer currently holds in the active file.

    Parameters
    ----------
    files : StoreFiles
        The writer state whose last published state is read.

    Returns
    -------
    ActiveHistory
        The active row count, its logical size and observation span, or an
        incomplete snapshot with a closed reason.
    """
    published = files._published
    if published is None:
        return ActiveHistory(has_metadata=False, reason="not_loaded")
    size = published.logical_bytes
    if size is None:
        return ActiveHistory(has_metadata=False, reason="unreadable")
    if published.reason is not None:
        return ActiveHistory(bytes=size, has_metadata=False, reason=published.reason)
    if not files.ready:
        return ActiveHistory(bytes=size, has_metadata=False, reason="not_loaded")
    # A published list only grows until the worker replaces it, and a replaced
    # list stops changing, so its published prefix is intact even while an
    # append lands. The slice copies the state the size belongs to.
    rows = tuple(published.rows[: published.count])
    return measured_history(rows, logical_bytes=size)
