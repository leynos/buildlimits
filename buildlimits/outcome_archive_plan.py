"""Pure planning for one cache-outcome archive run.

The planner reads an inventory scanned without reading segment content, so it
never opens a file and never blocks the writer. It decides which sources to
archive, which sidecars to rebuild, which orphans to quarantine, and which
closed history to trim or delete for the age and byte budgets. Retention evicts
oldest closed history first, whether it is a sealed source or an archive: a
budget that counts a sealed source's rows must be able to remove them, or the
plan would delete newer archives while older history survived. Every estimate
is conservative: a planned trim never frees more bytes than its sidecar proves.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_archive_budget import (
    ActiveHistory,
    Budget,
    budget_totals_are_unsettled,
    plan_byte_steps,
    plan_record_steps,
    planned_size,
    require_measurable_active,
)
from buildlimits.outcome_archive_trim import Trim
from buildlimits.utc_text import optional_utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segment_inventory import Inventory, SegmentEntry

__all__ = ["Plan", "PlanSummary", "plan_run"]


@dc.dataclass(frozen=True, slots=True)
class Plan:
    """The bounded set of operations one run should perform.

    Attributes
    ----------
    reconcile : tuple[tuple[SegmentEntry, SegmentEntry], ...]
        Sealed sources whose archive was already published.
    archive : tuple[SegmentEntry, ...]
        Sealed sources with no archive that retention does not evict.
    heal : tuple[SegmentEntry, ...]
        Archives whose sidecar is missing or corrupt.
    orphans : tuple[int, ...]
        Sequences whose sidecar has no archive.
    trims : tuple[tuple[SegmentEntry, Trim], ...]
        Archives to rewrite with a prefix trim.
    deletes : tuple[SegmentEntry, ...]
        Archives and sealed sources to remove entirely.
    retained_bytes : int
        Conservative logical bytes that will remain after the plan runs.
    cutoff : datetime | None
        The age cutoff applied, or None when age eviction is disabled.
    retention_deferred : bool
        Whether a configured budget was left to a follow-up plan, making the
        trims and deletes above provisional.
    """

    reconcile: tuple[tuple[SegmentEntry, SegmentEntry], ...] = ()
    archive: tuple[SegmentEntry, ...] = ()
    heal: tuple[SegmentEntry, ...] = ()
    orphans: tuple[int, ...] = ()
    trims: tuple[tuple[SegmentEntry, Trim], ...] = ()
    deletes: tuple[SegmentEntry, ...] = ()
    retained_bytes: int = 0
    cutoff: dt.datetime | None = None
    retention_deferred: bool = False


@dc.dataclass(frozen=True, slots=True)
class PlanSummary:
    """Counts and estimates that describe a plan without naming paths.

    Attributes
    ----------
    reconcile : int
        Sources whose existing archive will be checked.
    archive : int
        Sources that will be compressed.
    heal : int
        Sidecars that will be rebuilt.
    orphan : int
        Sidecars that will be quarantined.
    trim : int
        Archives that will be rewritten.
    delete : int
        Archives and sealed sources that will be removed.
    retained_bytes : int
        Conservative logical bytes remaining after the plan.
    cutoff : datetime | None
        The age cutoff applied.
    retention_deferred : bool
        Whether a configured budget is decided by a follow-up plan, so the trim
        and delete counts are not final.
    """

    reconcile: int = 0
    archive: int = 0
    heal: int = 0
    orphan: int = 0
    trim: int = 0
    delete: int = 0
    retained_bytes: int = 0
    cutoff: dt.datetime | None = None
    retention_deferred: bool = False

    @classmethod
    def of(cls, plan: Plan) -> PlanSummary:
        """Summarize one plan.

        Parameters
        ----------
        plan : Plan
            The plan to summarize.

        Returns
        -------
        PlanSummary
            The counts and estimates.
        """
        return cls(
            reconcile=len(plan.reconcile),
            archive=len(plan.archive),
            heal=len(plan.heal),
            orphan=len(plan.orphans),
            trim=len(plan.trims),
            delete=len(plan.deletes),
            retained_bytes=plan.retained_bytes,
            cutoff=plan.cutoff,
            retention_deferred=plan.retention_deferred,
        )

    def as_dict(self) -> dict[str, object]:
        """Return the stable JSON fields for a report.

        Returns
        -------
        dict[str, object]
            A JSON-compatible mapping of counts and estimates.
        """
        return {
            "reconcile": self.reconcile,
            "archive": self.archive,
            "heal": self.heal,
            "orphan": self.orphan,
            "trim": self.trim,
            "delete": self.delete,
            "retained_logical_bytes": self.retained_bytes,
            "cutoff": optional_utc_text(self.cutoff),
            "retention_deferred": self.retention_deferred,
        }


def plan_run(
    inventory: Inventory,
    policy: RetentionPolicy,
    *,
    now: dt.datetime,
    active: ActiveHistory,
) -> Plan:
    """Decide every operation one archive run should perform.

    Parameters
    ----------
    inventory : Inventory
        The read-only closed-history inventory.
    policy : RetentionPolicy
        The effective retention policy.
    now : datetime
        The caller-supplied UTC wall time.
    active : ActiveHistory
        What the writer's active file holds.

    Returns
    -------
    Plan
        Operations in execution order plus conservative estimates.

    Examples
    --------
    An empty inventory plans nothing and retains only the active bytes.
    >>> import datetime as dt
    >>> from buildlimits.outcome_archive_budget import ActiveHistory
    >>> from buildlimits.outcome_retention import RetentionPolicy
    >>> from buildlimits.outcome_segment_inventory import Inventory
    >>> now = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
    >>> plan_run(
    ...     Inventory(), RetentionPolicy(), now=now, active=ActiveHistory(bytes=7)
    ... ).retained_bytes
    7
    """
    duplicates = tuple(
        (sealed, archived)
        for sealed in inventory.sealed
        for archived in inventory.archived
        if sealed.sequence == archived.sequence
    )
    duplicated_sequences = {sealed.sequence for sealed, _archived in duplicates}
    heal = _heal_steps(inventory, duplicated_sequences)
    budget = Budget(policy=policy, cutoff=policy.cutoff(now), active=active)
    deletes, trims, deferred = _retention_steps(inventory, budget, duplicated_sequences)
    evicted = {entry.sequence for entry in deletes}
    # A source retention evicts is removed whole instead of being compressed
    # and then deleted: compression would only defer the same removal.
    archive = tuple(
        entry
        for entry in inventory.sealed
        if entry.sequence not in duplicated_sequences and entry.sequence not in evicted
    )
    retained = _retained_estimate(
        inventory, active_bytes=active.bytes, deletes=deletes, trims=trims
    )
    return Plan(
        reconcile=duplicates,
        archive=archive,
        heal=heal,
        orphans=inventory.orphan_sidecars,
        trims=trims,
        deletes=deletes,
        retained_bytes=retained,
        cutoff=budget.cutoff,
        retention_deferred=deferred,
    )


def _heal_steps(inventory: Inventory, duplicated: set[int]) -> tuple[SegmentEntry, ...]:
    """Return archives whose sidecar needs rebuilding.

    Returns
    -------
    tuple[SegmentEntry, ...]
        Archives with a missing or corrupt sidecar, excluding duplicates.
    """
    damaged = {*inventory.missing_sidecars, *inventory.corrupt_sidecars}
    return tuple(
        entry
        for entry in inventory.archived
        if entry.sequence in damaged and entry.sequence not in duplicated
    )


def _retention_steps(
    inventory: Inventory,
    budget: Budget,
    duplicated: set[int],
) -> tuple[tuple[SegmentEntry, ...], tuple[tuple[SegmentEntry, Trim], ...], bool]:
    """Decide age and byte eviction over closed history, oldest first.

    Sealed sources are candidates as well as archives, and both count towards
    the budgets: excluding a sealed source would make the plan delete newer
    archives while older history survived. A sealed source is only ever removed
    whole, and the follow-up plan trims it once this run has archived it.

    The byte and record walks run only when every surviving entry has been
    measured and no duplicated sequence is excluded from the candidates;
    otherwise their totals misstate what the store retains, and a whole
    deletion chosen against them could remove an older entry the settled
    store keeps. Age eviction is decided from the cutoff alone, so it is
    planned either way and the follow-up pass re-plans the budgets. A walk
    that stops at an entry it cannot yet settle defers its budget the same way.

    Returns
    -------
    tuple[tuple[SegmentEntry, ...], tuple[tuple[SegmentEntry, Trim], ...], bool]
        Entries to delete, archives to rewrite with a trim, and whether a
        configured budget was left to the follow-up plan.
    """
    candidates = tuple(
        entry for entry in inventory.effective if entry.sequence not in duplicated
    )
    deletes: dict[int, SegmentEntry] = {}
    if budget.cutoff is not None:
        for entry in candidates:
            _plan_age_step(entry, budget.cutoff, deletes)
    trims = _age_trims(candidates, budget.cutoff, deletes)
    measurable = _budgets_are_measurable(candidates, budget.cutoff, deletes, duplicated)
    settled = True
    if measurable:
        # Both walks always run: the record walk composes with the byte walk's
        # decisions, so a stopped byte walk must not skip the cap.
        settled = plan_byte_steps(candidates, budget, deletes, trims)
        settled = plan_record_steps(candidates, budget, deletes, trims) and settled
    else:
        # An unmeasurable cap is a run invariant rather than a planning
        # decision: even a pass that defers its walk has to report it.
        require_measurable_active(budget)
    # A later walk may delete an archive an earlier one already planned to trim;
    # rewriting a file that is about to be removed would only churn it, so a
    # deleted sequence is never also planned as a trim.
    trimmed = tuple(
        (entry, trims[entry.sequence])
        for entry in candidates
        if entry.sequence in trims and entry.sequence not in deletes
    )
    bounded = budget.policy.has_byte_limit or budget.policy.max_records is not None
    deferred = bounded and (not measurable or not settled)
    return tuple(deletes[sequence] for sequence in sorted(deletes)), trimmed, deferred


def _budgets_are_measurable(
    candidates: tuple[SegmentEntry, ...],
    cutoff: dt.datetime | None,
    deletes: cabc.Mapping[int, SegmentEntry],
    duplicated: set[int],
) -> bool:
    """Whether a budget walk can safely choose whole deletions this pass.

    A duplicated sequence is left out of the candidates while reconciliation
    settles it, so its rows would be missing from the total the walk acts on.
    ``budget_totals_are_unsettled`` reports the other conditions: an entry
    whose span crosses the cutoff, or one with no trustworthy measurement. A
    later pass re-plans from the reconciled and measured inventory.

    Returns
    -------
    bool
        True when none of those conditions applies.
    """
    if duplicated:
        return False
    return not budget_totals_are_unsettled(candidates, cutoff, deletes)


def _plan_age_step(
    entry: SegmentEntry, cutoff: dt.datetime, deletes: dict[int, SegmentEntry]
) -> None:
    """Record a whole-segment age deletion when every row is older.

    An entry without a trustworthy sidecar has neither a count nor a span, so
    no age decision can be made from it. The heal step repairs the sidecar,
    and the next firing plans from the span it rebuilt.
    """
    if not entry.has_metadata or entry.last_observed_at is None:
        return
    if entry.last_observed_at < cutoff:
        deletes[entry.sequence] = entry


def _age_trims(
    candidates: tuple[SegmentEntry, ...],
    cutoff: dt.datetime | None,
    deletes: dict[int, SegmentEntry],
) -> dict[int, Trim]:
    """Record the prefix trim a straddling archive needs.

    A sealed source that straddles the cutoff is left untrimmed here. This run
    archives it, and the follow-up plan applies the trim to the archive it
    becomes, which is the first point at which it can be rewritten.

    Returns
    -------
    dict[int, Trim]
        A trim keyed by sequence for every archive that straddles the cutoff.
    """
    if cutoff is None:
        return {}
    trims: dict[int, Trim] = {}
    for entry in candidates:
        if entry.sequence in deletes or not entry.has_metadata:
            continue
        if entry.state != "archived":
            continue
        first = entry.first_observed_at
        if first is not None and first < cutoff:
            trims[entry.sequence] = Trim(before=cutoff)
    return trims


def _retained_estimate(
    inventory: Inventory,
    *,
    active_bytes: int,
    deletes: tuple[SegmentEntry, ...],
    trims: tuple[tuple[SegmentEntry, Trim], ...],
) -> int:
    """Estimate the logical bytes retained after the planned eviction.

    Returns
    -------
    int
        Active bytes plus every surviving sealed source plus every surviving
        archive, with planned trims applied.
    """
    deleted = {entry.sequence for entry in deletes}
    trim_map = {entry.sequence: trim for entry, trim in trims}
    retained = active_bytes
    for entry in inventory.effective:
        if entry.sequence in deleted:
            continue
        if entry.state == "sealed":
            retained += entry.logical_bytes
        else:
            retained += planned_size(entry, trim_map.get(entry.sequence))
    return retained
