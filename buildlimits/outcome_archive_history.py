"""What the writer's active file holds at the moment a run plans.

The archive planner measures the retained store against two bounds, and both
count the rows the writer has not sealed. This value type carries that
snapshot from the scan that read it to the planner and the report: the whole
file's figures, plus the rows and bytes the age rule keeps when the scan was
asked to apply a cutoff. It lives apart from the budget walks so the reader,
the planner and the report can share one shape without depending on the
retention algorithms.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_active_history import (
    ActiveHistory as ActiveHistory,
)
from buildlimits.outcome_active_history import (
    measured_history as measured_history,
)

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_segment_inventory import Inventory

__all__ = [
    "ActiveHistory",
    "Observed",
    "SettledStore",
    "measured_history",
    "observed_history",
    "physical_fallback_bytes",
    "retained_logical_bytes",
]


@dc.dataclass(frozen=True, slots=True)
class SettledStore:
    """One settled reading of the store, with its retention decision.

    Attributes
    ----------
    inventory : Inventory
        The closed history the settled scan found.
    active : ActiveHistory
        The active file from the same reading.
    retention_unsettled : bool
        Whether retention still owes work after this reading.
    """

    inventory: Inventory
    active: ActiveHistory
    retention_unsettled: bool


@dc.dataclass(frozen=True, slots=True)
class Observed:
    """What closed metadata and the active file say about the retained rows."""

    records: int
    observed_from: dt.datetime | None
    observed_until: dt.datetime | None
    unmeasured: int


def retained_logical_bytes(inventory: Inventory, active: ActiveHistory) -> int:
    """Return the logical bytes the settled store currently holds.

    A sequence is counted once: ``Inventory.effective`` prefers a sealed source
    over its archive, so a publication whose source removal failed does not
    count both copies. The active file contributes the bytes it holds now.

    Returns
    -------
    int
        Logical bytes in the active file plus one entry per closed sequence.
    """
    return active.bytes + sum(entry.logical_bytes for entry in inventory.effective)


def physical_fallback_bytes(inventory: Inventory) -> int:
    """Return the part of the retained total taken from a physical file size.

    An archive whose sidecar is missing or untrusted has no known logical
    size, so the scan substitutes its compressed file size. The budget walks
    defer while such an archive survives rather than acting on the substitute,
    because a compressed size can be larger or smaller than the logical
    history. The figure is therefore not a conservative logical measurement:
    callers that print the total must say how much of it is a fallback.

    Returns
    -------
    int
        The summed logical figure of archives whose metadata is unavailable.
    """
    return sum(
        entry.logical_bytes
        for entry in inventory.effective
        if entry.state == "archived" and not entry.has_metadata
    )


def observed_history(inventory: Inventory, active: ActiveHistory) -> Observed:
    """Summarize every retained row the metadata and the active file describe.

    Returns
    -------
    Observed
        Retained record count, earliest and latest observation, and the number
        of effective entries whose metadata was missing. An entry without
        trustworthy metadata contributes no rows and no span rather than
        guessing, but it is counted so a report never claims completeness.
    """
    described = [
        entry for entry in inventory.effective if entry.record_count is not None
    ]
    records = sum(entry.record_count or 0 for entry in described) + active.records
    unmeasured = sum(1 for entry in inventory.effective if entry.record_count is None)
    unmeasured += 0 if active.has_metadata else 1
    first = [entry.first_observed_at for entry in described if entry.first_observed_at]
    last = [entry.last_observed_at for entry in described if entry.last_observed_at]
    if active.first_observed_at is not None:
        first.append(active.first_observed_at)
    if active.last_observed_at is not None:
        last.append(active.last_observed_at)
    return Observed(
        records=records,
        observed_from=min(first, default=None),
        observed_until=max(last, default=None),
        unmeasured=unmeasured,
    )
