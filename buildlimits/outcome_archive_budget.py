"""Logical-byte planning for the archives an archive run retains.

The byte budget counts uncompressed history, so an archive's contribution is
the size its sidecar records rather than the XZ file's own size. Planning
deletes whole entries oldest-first and only then trims the straddler; every
estimate is conservative, never freeing more bytes than a sidecar proves.

A sealed source counts towards both budgets and can be evicted whole. In an
archive run it is never trimmed: it is still uncompressed, so a straddler ends
the walk and the follow-up plan trims the archive it becomes. A retention-only
run rewrites the plain source in place, so the same walk may give it a prefix
trim without compressing it. The writer's active rows count too, but only those
the age rule keeps: a row the writer will compact away on its next idle tick is
not retained history, and counting it would evict eligible closed history to
make room for bytes retention has already expired.

A count trim is only planned for an archive that no time or byte trim already
rewrites. Those trims drop rows the planner never counted, at positions a
prefix count cannot target: composing both would drop the count's leading rows
and the rows the other rule removes. Such a straddler ends the count walk; the
follow-up plan applies the count to the measured document that remains.

The walks are only sound over measured history. A time trim removes rows no
planner can count, so a total that still includes them overstates what the
store retains. An archive with no trustworthy sidecar falls back to its
compressed file's size, and a sealed source with no writer metadata has its
bytes counted but not its span; neither describes the history it holds. A
whole deletion chosen against such a total could discard history the settled
store keeps, so the walks run only once every surviving entry is measured;
the follow-up plan runs them after the trim, rebuild and archival. The writer's
active file is part of the same total: when its surviving rows cannot be read,
an age-aware byte budget is deferred rather than decided from the whole file.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_archive_history import ActiveHistory
from buildlimits.outcome_archive_sizes import planned_records, planned_size
from buildlimits.outcome_archive_trim import Trim

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segment_inventory import SegmentEntry

__all__ = [
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
    cannot show the retained store meets the cap. The run reports an
    operational failure instead of counting the file as empty, which would
    make any store look capped.
    """


@dc.dataclass(frozen=True, slots=True)
class Budget:
    """The policy, cutoff and unsealed history one plan is measured against.

    Attributes
    ----------
    policy : RetentionPolicy
        The effective retention policy.
    cutoff : datetime | None
        The age cutoff, or None when no age limit is configured or the clock
        guard withheld it.
    active : ActiveHistory
        What the writer's active file holds. The candidates cover the closed
        history the writer sealed but the archiver has not compressed.
    can_trim_sealed : bool
        Whether a sealed source may be rewritten in place rather than ending
        the walk. Only a retention-only run sets this.
    """

    policy: RetentionPolicy
    cutoff: dt.datetime | None
    active: ActiveHistory = dc.field(default_factory=ActiveHistory)
    can_trim_sealed: bool = False


def plan_byte_steps(
    candidates: tuple[SegmentEntry, ...],
    budget: Budget,
    deletes: dict[int, SegmentEntry],
    trims: dict[int, Trim],
) -> bool:
    """Evict the oldest entries until the logical byte budget is met.

    ``deletes`` and ``trims`` are updated in place. The walk is shared with
    the record cap and documented in ``_BudgetWalk.evict``.

    Returns
    -------
    bool
        Whether this pass settled the budget. A walk that stopped at a source
        it cannot trim, or that exhausted closed history with bytes still to
        free, leaves the remainder to the follow-up plan.
    """
    if not budget.policy.has_byte_limit:
        return True
    walk = _BudgetWalk(
        candidates, deletes, trims, can_trim_sealed=budget.can_trim_sealed
    )
    retained = budget.active.budget_bytes + sum(
        size for _, size in walk.surviving(planned_size)
    )
    return walk.evict(retained - budget.policy.max_bytes, planned_size, _byte_trim)


def require_measurable_active(budget: Budget) -> None:
    """Refuse a configured record cap whose active rows are unknown.

    The cap counts the writer's unsealed rows. A plan that cannot read them
    cannot show the retained store meets the cap, even when it evicts nothing,
    so the check is separate from the walk: a deferring pass still reports it.

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
) -> bool:
    """Evict the oldest entries until the retained record cap is met.

    Only an explicitly configured cap applies, and it bounds the whole
    retained store: the active rows the age rule keeps and every closed
    entry's rows count towards it. A sealed source is closed history and is
    evicted whole like an archive. ``deletes`` and ``trims`` are updated in
    place, as the byte budget does. ``require_measurable_active`` raises when
    the active rows were not read: no plan can show the store is within a cap
    it cannot count against.

    Returns
    -------
    bool
        Whether this pass settled the cap, as ``plan_byte_steps`` reports.
    """
    require_measurable_active(budget)
    max_records = budget.policy.max_records
    if max_records is None:
        return True
    walk = _BudgetWalk(
        candidates, deletes, trims, can_trim_sealed=budget.can_trim_sealed
    )
    retained = budget.active.budget_records
    retained += sum(
        records
        for _entry, records in walk.surviving(planned_records)
        if records is not None
    )
    return walk.evict(retained - max_records, planned_records, _record_trim)


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
    entry with no trustworthy metadata may be counted wrongly: an archive
    falls back to its XZ size, which the header can make larger than its
    content, and a sealed source has bytes but no span, hiding a pending age
    eviction. The run repairs both, then the follow-up pass measures them.

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
        and (not entry.has_metadata or _straddles_cutoff(entry, cutoff))
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
    step reads and writes all three. The maps are the planners' own and are
    updated in place, so a walk sees the other rules' decisions.

    Attributes
    ----------
    candidates : tuple[SegmentEntry, ...]
        The closed entries the scan found, oldest first.
    deletes : dict[int, SegmentEntry]
        The entries already planned for deletion, updated in place.
    trims : dict[int, Trim]
        The trims already planned, by sequence, updated in place.
    can_trim_sealed : bool
        Whether a sealed source may be given a prefix trim in place.
    """

    candidates: tuple[SegmentEntry, ...]
    deletes: dict[int, SegmentEntry]
    trims: dict[int, Trim]
    can_trim_sealed: bool = False

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
    ) -> bool:
        """Delete whole entries oldest-first, then trim the one that straddles.

        An entry is deleted while its planned value fits inside the remaining
        excess, and the walk stops as soon as the excess is covered rather than
        churning a later archive. The first entry that does not fit is trimmed,
        so no later entry is rewritten when the budget is met. In an archive
        run a sealed source never gets an allowance, because it cannot be
        rewritten until this run has compressed it; a retention-only run can
        rewrite it in place. An unplannable entry ends the walk: evicting newer
        history ahead of older history of unknown count would break
        oldest-first retention. A trim callable may decline to compose too.

        Parameters
        ----------
        excess : int
            The planned value that must be freed.
        keep : Callable[[SegmentEntry, Trim | None], int | None]
            The planned value of one entry, or None when it cannot be planned.
        trim_straddler : Callable[[Trim, int, int], Trim | None]
            The updated trim for the first entry that does not fit, or None
            when this walk must leave the entry to a later plan.

        Returns
        -------
        bool
            Whether this pass decided everything the budget needs. False when
            the walk stopped at an entry it cannot measure or rewrite yet, or
            when closed history ran out with excess still to free. Retention
            cannot trim the active file, so an excess that only the active
            rows create stays for a later pass to report rather than being
            counted as settled.
        """
        for entry, value in self.surviving(keep):
            if excess <= 0:
                break
            if value is None:
                return False
            if value <= excess:
                self.deletes[entry.sequence] = entry
                excess -= value
                continue
            if entry.state == "sealed" and not self.can_trim_sealed:
                return False
            trim = trim_straddler(self.trims.get(entry.sequence, Trim()), value, excess)
            if trim is None:
                return False
            self.trims[entry.sequence] = trim
            # A straddler trim is planned to free exactly the excess it was
            # given, so nothing remains to cover.
            excess = 0
            break
        return excess <= 0


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
    rule removes: *at cutoff, expired, fresh* capped at two rows needs no count
    trim once the expired row leaves, while a composed trim would drop the
    cutoff row too. The follow-up plan applies the count to what remains.

    Returns
    -------
    Trim | None
        The previous trim with the leading row count raised to the excess, or
        None when another rule must be applied and measured first.
    """
    if existing.before is not None or existing.allowed_bytes is not None:
        return None
    return dc.replace(existing, records=max(existing.records, excess))
