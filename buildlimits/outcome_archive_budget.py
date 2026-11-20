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

__all__ = [
    "Budget",
    "plan_byte_steps",
    "plan_record_steps",
    "planned_records",
    "planned_size",
]


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
        size for _, size in _surviving(candidates, deletes, trims, planned_size)
    )
    excess = retained - budget.policy.max_bytes
    if excess <= 0:
        return
    for entry, size in _surviving(candidates, deletes, trims, planned_size):
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


def plan_record_steps(
    candidates: tuple[SegmentEntry, ...],
    max_records: int | None,
    deletes: dict[int, SegmentEntry],
    trims: dict[int, Trim],
) -> None:
    """Evict the oldest archives until the retained record cap is met.

    Only an explicitly configured cap applies, and only to closed history: the
    writer owns the active file, so its rows become evictable once it seals
    them. An archive with no trustworthy sidecar has no known count and is
    skipped; the heal step runs before retention on the next pass.

    ``deletes`` and ``trims`` are updated in place, exactly as the byte budget
    does, and a whole archive is deleted while it fits inside the excess.
    """
    if max_records is None:
        return
    retained = sum(
        records
        for entry, records in _surviving(candidates, deletes, trims, planned_records)
        if records is not None
    )
    excess = retained - max_records
    if excess <= 0:
        return
    for entry, records in _surviving(candidates, deletes, trims, planned_records):
        if excess <= 0:
            return
        if records is None:
            # Without a sidecar the count cannot be planned, and guessing
            # would either overstate or understate the cap. The sidecar is
            # repaired before the next retention pass.
            continue
        if records <= excess:
            deletes[entry.sequence] = entry
            excess -= records
            continue
        existing = trims.get(entry.sequence, Trim())
        trims[entry.sequence] = dc.replace(
            existing, records=max(existing.records, excess)
        )
        return


def planned_records(entry: SegmentEntry, trim: Trim | None) -> int | None:
    """Estimate the records one archive keeps after its trim.

    Returns
    -------
    int | None
        The sidecar's count less a leading count trim, or None when no
        trustworthy sidecar describes the archive. A time or byte trim cannot
        be estimated without reading rows, so it is treated as freeing no
        records.
    """
    if entry.record_count is None:
        return None
    if trim is None:
        return entry.record_count
    return max(entry.record_count - trim.records, 0)


def _surviving[Kept](
    candidates: tuple[SegmentEntry, ...],
    deletes: cabc.Mapping[int, SegmentEntry],
    trims: cabc.Mapping[int, Trim],
    keep: cabc.Callable[[SegmentEntry, Trim | None], Kept],
) -> cabc.Iterator[tuple[SegmentEntry, Kept]]:
    """Yield each surviving candidate with the value ``keep`` estimates.

    The delete set and the trims are read as each entry is yielded, so a
    planner that changes them while walking still sees its own decisions.

    Parameters
    ----------
    candidates : tuple[SegmentEntry, ...]
        The archives the scan found, oldest first.
    deletes : Mapping[int, SegmentEntry]
        The archives already planned for deletion.
    trims : Mapping[int, Trim]
        The trims already planned, by sequence.
    keep : Callable[[SegmentEntry, Trim | None], _Kept]
        The estimate to report for one surviving archive.

    Returns
    -------
    Iterator[tuple[SegmentEntry, Kept]]
        Each archive no step has deleted yet, with its planned value.
    """
    return (
        (entry, keep(entry, trims.get(entry.sequence)))
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
