"""Combine the closed-history inventory with the active file.

The archive command owns both halves of the retained history, so it is the
only component that can answer "what does the policy still retain?" across
active, sealed and archived sources. The pure value shapes live in
:mod:`buildlimits.outcome_retention_report`; this module adds the one
inventory-dependent combination, keeping the stock inventory adapter out of the
pure-domain import closure.
"""

import typing as typ

from buildlimits.outcome_retention_report import ActiveHistory, RetainedHistory

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_segment_inventory import Inventory, SegmentEntry

__all__ = ["retained_history"]


def retained_history(
    inventory: Inventory, active: ActiveHistory | None = None
) -> RetainedHistory:
    """Combine closed history and the active file into one retained view.

    A closed segment whose metadata is missing still contributes its logical
    bytes, because the file is retained either way, but its rows and times are
    unknown. The result then reports ``complete=False`` so a caller never
    presents a lower bound as an exact count.

    Parameters
    ----------
    inventory : Inventory
        The read-only closed-history inventory.
    active : ActiveHistory | None
        The active-file snapshot, or None when it was not measured.

    Returns
    -------
    RetainedHistory
        The retained counts, bytes and observation span.

    Examples
    --------
    >>> from buildlimits.outcome_segment_inventory import Inventory
    >>> retained_history(Inventory()).record_count
    0
    """
    measured = ActiveHistory() if active is None else active
    entries = inventory.effective
    sealed_bytes = _logical_bytes(entries, "sealed")
    archived_logical = _logical_bytes(entries, "archived")
    record_count = sum(entry.record_count or 0 for entry in entries)
    first = _extreme(
        min,
        *(entry.first_observed_at for entry in entries),
        measured.first_observed_at,
    )
    last = _extreme(
        max,
        *(entry.last_observed_at for entry in entries),
        measured.last_observed_at,
    )
    return RetainedHistory(
        record_count=record_count + measured.record_count,
        logical_bytes=measured.logical_bytes + sealed_bytes + archived_logical,
        active_bytes=measured.logical_bytes,
        sealed_bytes=sealed_bytes,
        archived_logical_bytes=archived_logical,
        archived_physical_bytes=sum(
            entry.physical_bytes for entry in inventory.archived
        ),
        closed_segments=len(entries),
        active_records=measured.record_count,
        first_observed_at=first,
        last_observed_at=last,
        complete=measured.complete and all(entry.has_metadata for entry in entries),
    )


def _logical_bytes(entries: cabc.Iterable[SegmentEntry], state: str) -> int:
    """Total the uncompressed bytes a closed history state retains.

    Parameters
    ----------
    entries : Iterable[SegmentEntry]
        Every closed segment the inventory reports.
    state : str
        The closed state to total: ``"sealed"`` or ``"archived"``.

    Returns
    -------
    int
        The logical bytes held in that state.
    """
    return sum(entry.logical_bytes for entry in entries if entry.state == state)


def _extreme(
    pick: cabc.Callable[[list[dt.datetime]], dt.datetime],
    *times: dt.datetime | None,
) -> dt.datetime | None:
    """Return the earliest or latest known moment, ignoring unknown ones.

    A segment without metadata, or an active file that could not be measured,
    contributes no moment. The span then covers only the history that was
    actually observed rather than inventing a boundary.

    Returns
    -------
    datetime | None
        The extreme value, or None when every value is unknown.
    """
    known = [value for value in times if value is not None]
    return pick(known) if known else None
