"""Logical-byte planning for the archives an archive run retains.

The byte budget counts uncompressed history, so an archive's contribution is
the size its sidecar records rather than the XZ file's own size. Planning
deletes whole archives oldest-first and only then trims the one archive that
straddles the remaining allowance; every estimate is conservative, because a
planned trim never frees more bytes than its sidecar proves.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_archive_verify import Trim

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segment_inventory import SegmentEntry

__all__ = ["Budget", "plan_byte_steps", "planned_size"]


@dc.dataclass(frozen=True, slots=True)
class Budget:
    """The policy, cutoff and active bytes one plan is measured against.

    Attributes
    ----------
    policy : RetentionPolicy
        The effective retention policy.
    cutoff : datetime | None
        The age cutoff, or None when no age limit is configured.
    active_bytes : int
        Logical bytes in the writer's active file.
    """

    policy: RetentionPolicy
    cutoff: dt.datetime | None
    active_bytes: int


def plan_byte_steps(
    candidates: tuple[SegmentEntry, ...],
    budget: Budget,
    deletes: dict[int, SegmentEntry],
    trims: dict[int, Trim],
) -> None:
    """Evict the oldest archives until the logical byte budget is met.

    ``deletes`` and ``trims`` are updated in place. A whole archive is deleted
    while it fits inside the excess; the first archive that does not is given
    a byte allowance and planning stops, so no later archive is rewritten when
    the budget is already met.
    """
    if not budget.policy.has_byte_limit:
        return
    retained = budget.active_bytes + sum(
        size for _, size in _planned_entries(candidates, deletes, trims)
    )
    excess = retained - budget.policy.max_bytes
    if excess <= 0:
        return
    for entry, size in _planned_entries(candidates, deletes, trims):
        if excess <= 0:
            # A whole-archive deletion may have met the budget exactly. The
            # next archive then needs no rewrite, and trimming it to its own
            # size would churn the file without dropping a row.
            return
        if size <= excess:
            deletes[entry.sequence] = entry
            excess -= size
            continue
        existing = trims.get(entry.sequence, Trim())
        trims[entry.sequence] = dc.replace(
            existing, allowed_bytes=size - excess, source_bytes=size
        )
        return


def _planned_entries(
    candidates: tuple[SegmentEntry, ...],
    deletes: dict[int, SegmentEntry],
    trims: dict[int, Trim],
) -> cabc.Iterator[tuple[SegmentEntry, int]]:
    """Yield each surviving candidate with the logical bytes it keeps.

    The delete set and the trims are read as each entry is yielded, so a
    planner that changes them while walking still sees its own decisions.

    Returns
    -------
    Iterator[tuple[SegmentEntry, int]]
        The archive no step has deleted yet, with its planned size.
    """
    return (
        (entry, planned_size(entry, trims.get(entry.sequence)))
        for entry in candidates
        if entry.sequence not in deletes
    )


def planned_size(entry: SegmentEntry, trim: Trim | None) -> int:
    """Estimate the logical bytes one archive keeps after its trim.

    Returns
    -------
    int
        The sidecar's logical size, capped by an explicit byte allowance. A
        time-based trim cannot be estimated without reading rows, so it is
        treated as freeing nothing.
    """
    if trim is None or trim.allowed_bytes is None:
        return entry.logical_bytes
    return min(entry.logical_bytes, trim.allowed_bytes)
