"""Logical-byte planning for the archives an archive run retains.

The byte budget counts uncompressed history, so an archive's contribution is
the size its sidecar records rather than the XZ file's own size. Planning
deletes whole entries oldest-first and only then trims the one archive that
straddles the remaining allowance; every estimate is conservative, because a
planned trim never frees more bytes than its sidecar proves.

A sealed source counts towards both budgets and can be evicted whole, but it
is never trimmed: it is still uncompressed, so a straddler ends the walk and
the follow-up plan trims the archive it becomes.

A count trim is only planned for an archive that no time or byte trim already
rewrites. Those trims drop rows the planner never counted, at positions a
prefix count cannot target: composing both would drop the count's leading rows
in addition to the rows the other rule removes, discarding rows the policy
allows. Such a straddler ends the count walk, and the follow-up plan applies
the count to the measured document the other trim leaves behind.

The walks are only sound over measured history. A time trim removes rows no
planner can count, so a total that still includes them overstates what the
store will retain, and an archive with no trustworthy sidecar is counted at
its compressed file's size, which need not describe the history it holds.
Deleting a whole older entry against either total would discard history the
settled store keeps, so the walks run only once every surviving entry has
been measured; the follow-up plan runs them after the trim and the rebuild.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_archive_trim import Trim

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segment_inventory import SegmentEntry

__all__ = [
    "ActiveHistory",
    "Budget",
    "UnknownActiveHistoryError",
    "budget_totals_are_unsettled",
    "plan_byte_steps",
    "plan_record_steps",
    "planned_records",
    "planned_size",
    "require_measurable_active",
]


class UnknownActiveHistoryError(Exception):
    """A record cap was configured but the active file's rows are unknown.

    The cap counts the writer's unsealed rows, so a plan that cannot read them
    cannot show that the retained store meets the cap. The run reports this as
    an operational failure instead of counting the active file as empty, which
    would make any store look capped.
    """


@dc.dataclass(frozen=True, slots=True)
class ActiveHistory:
    """What the writer's active file holds at the moment a run plans.

    Both bounds measure the whole retained store, so the rows the writer has
    not yet sealed are counted exactly as closed archives are. The writer owns
    the file, so this is a snapshot: it can only be as current as the scan that
    produced it.

    Attributes
    ----------
    bytes : int
        Logical bytes in the active file.
    records : int
        Record rows in the active file, excluding the header.
    first_observed_at : datetime | None
        Earliest row observation in the active file, when one was read.
    last_observed_at : datetime | None
        Latest row observation in the active file, when one was read.
    has_metadata : bool
        Whether the rows and span were read. A file that cannot be read at all
        contributes its size alone, and is reported as unmeasured.
    """

    bytes: int = 0
    records: int = 0
    first_observed_at: dt.datetime | None = None
    last_observed_at: dt.datetime | None = None
    has_metadata: bool = True


@dc.dataclass(frozen=True, slots=True)
class Budget:
    """The policy, cutoff and unsealed history one plan is measured against.

    Attributes
    ----------
    policy : RetentionPolicy
        The effective retention policy.
    cutoff : datetime | None
        The age cutoff, or None when no age limit is configured.
    active : ActiveHistory
        What the writer's active file holds. The candidates passed to the
        planners cover the closed history the writer has sealed but the
        archiver has not yet compressed, so no separate sealed count is needed.
    """

    policy: RetentionPolicy
    cutoff: dt.datetime | None
    active: ActiveHistory = dc.field(default_factory=ActiveHistory)


def plan_byte_steps(
    candidates: tuple[SegmentEntry, ...],
    budget: Budget,
    deletes: dict[int, SegmentEntry],
    trims: dict[int, Trim],
) -> None:
    """Evict the oldest entries until the logical byte budget is met.

    ``deletes`` and ``trims`` are updated in place. The walk is shared with the
    record cap and documented in ``_BudgetWalk.evict``; only the measure and
    the straddling archive's trim differ between the two budgets.
    """
    if not budget.policy.has_byte_limit:
        return
    walk = _BudgetWalk(candidates, deletes, trims)
    retained = budget.active.bytes + sum(
        size for _, size in walk.surviving(planned_size)
    )
    walk.evict(retained - budget.policy.max_bytes, planned_size, _byte_trim)


def require_measurable_active(budget: Budget) -> None:
    """Refuse a configured record cap whose active rows are unknown.

    The cap counts the writer's unsealed rows. A plan that cannot read them
    cannot show the retained store meets the cap, whether or not this pass
    decides to evict anything, so the check is separate from the walk: a pass
    that defers its budget planning still has to report the unmeasurable cap.

    Raises
    ------
    UnknownActiveHistoryError
        If a cap is configured and the active file's rows were not read.
    """
    if budget.policy.max_records is None or budget.active.has_metadata:
        return
    msg = "the active history cannot be counted, so the record cap is unmeasurable"
    raise UnknownActiveHistoryError(msg)


def plan_record_steps(
    candidates: tuple[SegmentEntry, ...],
    budget: Budget,
    deletes: dict[int, SegmentEntry],
    trims: dict[int, Trim],
) -> None:
    """Evict the oldest entries until the retained record cap is met.

    Only an explicitly configured cap applies, and it bounds the whole
    retained store: the active file's rows and every closed entry's rows count
    towards it. A sealed source is closed history and is evicted whole like an
    archive. ``deletes`` and ``trims`` are updated in place, exactly as the
    byte budget does.

    The cap's precondition is checked by ``require_measurable_active``, which
    raises when the active file's rows were not read: no plan can show the
    store is within a cap it cannot count against.
    """
    require_measurable_active(budget)
    max_records = budget.policy.max_records
    if max_records is None:
        return
    walk = _BudgetWalk(candidates, deletes, trims)
    retained = budget.active.records
    retained += sum(
        records
        for _entry, records in walk.surviving(planned_records)
        if records is not None
    )
    walk.evict(retained - max_records, planned_records, _record_trim)


def budget_totals_are_unsettled(
    candidates: tuple[SegmentEntry, ...],
    cutoff: dt.datetime | None,
    deletes: cabc.Mapping[int, SegmentEntry],
) -> bool:
    """Whether a surviving entry's planned value depends on a measurement.

    The walks act on the counts and sizes their sidecars describe, so a total
    is only trustworthy when every surviving entry has been measured. Two
    states break that. An entry whose span crosses the cutoff still holds rows
    a time trim removes, so the total counts rows that will not survive. An
    archive with no trustworthy sidecar is counted at its XZ file's size,
    which is not the history it holds: the container's own header makes a
    small document's file larger than its content, so that fallback can
    overstate the store as well as understate it. The run repairs both -- it
    applies the age trim and rebuilds the sidecar -- and the follow-up pass
    measures the result.

    Parameters
    ----------
    candidates : tuple[SegmentEntry, ...]
        The closed entries the pass may retain, oldest first.
    cutoff : datetime | None
        The age cutoff, or None when no age limit is configured.
    deletes : Mapping[int, SegmentEntry]
        The entries already planned for deletion.

    Returns
    -------
    bool
        True when a surviving entry's planned value is not yet measured.
    """
    return any(
        entry.sequence not in deletes
        and (
            (entry.state == "archived" and not entry.has_metadata)
            or _straddles_cutoff(entry, cutoff)
        )
        for entry in candidates
    )


def _straddles_cutoff(entry: SegmentEntry, cutoff: dt.datetime | None) -> bool:
    """Whether an entry still holds rows an age trim removes.

    Returns
    -------
    bool
        True when the entry's earliest row is older than the cutoff.
    """
    if cutoff is None or entry.first_observed_at is None:
        return False
    return entry.first_observed_at < cutoff


@dc.dataclass(slots=True)
class _BudgetWalk:
    """One budget's walk over the closed entries, with its decisions so far.

    The candidates and the two decision maps travel together because every
    step of the walk reads and writes all three. The maps are the planners'
    own and are updated in place, so a walk sees the age cutoff's decisions
    and the other budget's.

    Attributes
    ----------
    candidates : tuple[SegmentEntry, ...]
        The closed entries the scan found, oldest first.
    deletes : dict[int, SegmentEntry]
        The entries already planned for deletion, updated in place.
    trims : dict[int, Trim]
        The trims already planned, by sequence, updated in place.
    """

    candidates: tuple[SegmentEntry, ...]
    deletes: dict[int, SegmentEntry]
    trims: dict[int, Trim]

    def surviving[Kept](
        self, keep: cabc.Callable[[SegmentEntry, Trim | None], Kept]
    ) -> cabc.Iterator[tuple[SegmentEntry, Kept]]:
        """Yield each surviving candidate with the value ``keep`` estimates.

        Parameters
        ----------
        keep : Callable[[SegmentEntry, Trim | None], _Kept]
            The estimate to report for one surviving entry.

        Returns
        -------
        Iterator[tuple[SegmentEntry, Kept]]
            Each entry no step has deleted yet, with its planned value.
        """
        return (
            (entry, keep(entry, self.trims.get(entry.sequence)))
            for entry in self.candidates
            if entry.sequence not in self.deletes
        )

    def evict(
        self,
        excess: int,
        keep: cabc.Callable[[SegmentEntry, Trim | None], int | None],
        trim_straddler: cabc.Callable[[Trim, int, int], Trim | None],
    ) -> None:
        """Delete whole entries oldest-first, then trim the one that straddles.

        An entry is deleted while its planned value fits inside the remaining
        excess, and the walk stops as soon as the excess is covered rather than
        churning a later archive. The first entry that does not fit is trimmed
        instead, so no later entry is rewritten when the budget is met. A
        sealed source never gets an allowance, because it cannot be rewritten
        until this run has compressed it, and an entry whose value cannot be
        planned ends the walk: evicting newer history ahead of older history
        whose count is merely unknown would break oldest-first retention. A
        trim callable may likewise decline to compose, which leaves the
        decision to the follow-up plan.

        Parameters
        ----------
        excess : int
            The planned value that must be freed.
        keep : Callable[[SegmentEntry, Trim | None], int | None]
            The planned value of one entry, or None when it cannot be planned.
        trim_straddler : Callable[[Trim, int, int], Trim | None]
            The updated trim for the first entry that does not fit, or None
            when this walk must leave the entry to a later plan.
        """
        if excess <= 0:
            return
        for entry, value in self.surviving(keep):
            if excess <= 0:
                return
            if value is None:
                return
            if value <= excess:
                self.deletes[entry.sequence] = entry
                excess -= value
                continue
            if entry.state == "sealed":
                return
            trim = trim_straddler(self.trims.get(entry.sequence, Trim()), value, excess)
            if trim is None:
                return
            self.trims[entry.sequence] = trim
            return


def _byte_trim(existing: Trim, size: int, excess: int) -> Trim:
    """Allow one straddling archive the bytes that fit inside the excess.

    Returns
    -------
    Trim
        The previous trim with the byte allowance and source size set.
    """
    return dc.replace(existing, allowed_bytes=size - excess, source_bytes=size)


def _record_trim(existing: Trim, _records: int, excess: int) -> Trim | None:
    """Cap one straddling archive at the row count that fits in the excess.

    A trim that already drops rows by time or by bytes is left for the
    follow-up plan instead. The count is a prefix over the document's row
    positions, and the planner cannot know how many of that prefix the other
    rule removes: a document ordered *at cutoff, expired, fresh*, capped at two
    rows, needs no count trim at all once the expired row leaves, while a
    composed trim would drop the cutoff row too. Once this run has applied the
    other trim, the follow-up plan reads the surviving count from the sidecar
    and applies the count to the document that remains.

    Returns
    -------
    Trim | None
        The previous trim with the leading row count raised to the excess, or
        None when another rule must be applied and measured first.
    """
    if existing.before is not None or existing.allowed_bytes is not None:
        return None
    return dc.replace(existing, records=max(existing.records, excess))


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


def planned_size(entry: SegmentEntry, trim: Trim | None) -> int:
    """Estimate the logical bytes one entry keeps after its trim.

    Returns
    -------
    int
        The recorded logical size, capped by an explicit byte allowance. A
        time-based trim cannot be estimated without reading rows, so it is
        treated as freeing nothing.
    """
    if trim is None or trim.allowed_bytes is None:
        return entry.logical_bytes
    return min(entry.logical_bytes, trim.allowed_bytes)
