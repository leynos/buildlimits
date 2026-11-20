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

from buildlimits.outcome_archive_age import age_trims, plan_age_step
from buildlimits.outcome_archive_budget import (
    Budget,
    budget_totals_are_unsettled,
    plan_byte_steps,
    plan_record_steps,
    require_measurable_active,
)
from buildlimits.outcome_archive_sizes import retained_estimate
from buildlimits.utc_text import optional_utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_archive_trim import Trim
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
        Archives to remove entirely.
    eviction_causes : tuple[tuple[int, str], ...]
        The closed bound (``age``, ``bytes`` or ``records``) that made each
        planned deletion eligible, sorted by sequence. The cause is recorded
        as the planner applies each bound, so a report never attributes an
        eviction to a bound that is disabled. A trim names its own bounds
        through the fields it carries.
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
    eviction_causes: tuple[tuple[int, str], ...] = ()
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
        What the writer's active file holds, measured against the age cutoff
        when one applies so only the rows retention keeps count.

    Returns
    -------
    Plan
        Operations in execution order plus conservative estimates.

    Examples
    --------
    An empty inventory plans nothing and retains only the active bytes.
    >>> import datetime as dt
    >>> from buildlimits.outcome_archive_history import ActiveHistory
    >>> from buildlimits.outcome_retention import RetentionPolicy
    >>> from buildlimits.outcome_segment_inventory import Inventory
    >>> now = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
    >>> plan_run(
    ...     Inventory(), RetentionPolicy(), now=now, active=ActiveHistory(bytes=7)
    ... ).retained_bytes
    7
    """
    duplicates = _duplicates(inventory)
    duplicated_sequences = {sealed.sequence for sealed, _archived in duplicates}
    heal = _heal_steps(inventory, duplicated_sequences)
    budget = Budget(policy=policy, cutoff=policy.cutoff(now), active=active)
    deletes, trims, causes, deferred = _retention_steps(
        inventory, budget, duplicated_sequences
    )
    evicted = {entry.sequence for entry in deletes}
    # A source retention evicts is removed whole instead of being compressed
    # and then deleted: compression would only defer the same removal.
    archive = tuple(
        entry
        for entry in inventory.sealed
        if entry.sequence not in duplicated_sequences and entry.sequence not in evicted
    )
    retained = retained_estimate(
        inventory,
        active_bytes=active.budget_bytes,
        deletes=deletes,
        trims=trims,
    )
    return Plan(
        reconcile=duplicates,
        archive=archive,
        heal=heal,
        orphans=inventory.orphan_sidecars,
        trims=trims,
        deletes=deletes,
        eviction_causes=causes,
        retained_bytes=retained,
        cutoff=budget.cutoff,
        retention_deferred=deferred,
    )


def _duplicates(
    inventory: Inventory,
) -> tuple[tuple[SegmentEntry, SegmentEntry], ...]:
    """Pair each sealed source with an archive of the same sequence.

    One lookup pass keeps duplicate detection linear. A store with a thousand
    sealed sources beside ten thousand archives would otherwise compare every
    pair on each plan, and a reconciliation is followed by more plans.

    Returns
    -------
    tuple[tuple[SegmentEntry, SegmentEntry], ...]
        Sealed entries with their archive, in sealed-entry order.
    """
    archived_by_sequence = {entry.sequence: entry for entry in inventory.archived}
    return tuple(
        (sealed, archived_by_sequence[sealed.sequence])
        for sealed in inventory.sealed
        if sealed.sequence in archived_by_sequence
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
) -> tuple[
    tuple[SegmentEntry, ...],
    tuple[tuple[SegmentEntry, Trim], ...],
    tuple[tuple[int, str], ...],
    bool,
]:
    """Decide age, byte and record eviction over closed history, oldest first.

    Sealed sources are candidates as well as archives, and both count towards
    the budgets: excluding a sealed source would make the plan delete newer
    archives while older history survived. A sealed source is only ever removed
    whole, and the follow-up plan trims it once this run has archived it.

    The byte and record walks run only when every surviving entry has been
    measured, the active file's surviving rows were read, and no duplicated
    sequence is excluded from the candidates; otherwise their totals misstate
    what the store retains, and a whole deletion chosen against them could
    remove an older entry the settled store keeps. Age eviction is decided from
    the cutoff alone, so it is planned either way and the follow-up pass
    re-plans the budgets. A walk that stops at an entry it cannot yet settle
    defers its budget the same way.

    Each bound is applied in turn and the deletions it plans are attributed to
    it, so the report names the bound that actually removed an entry. Age runs
    first, then bytes, then the record cap: an entry the age plan already
    removed keeps ``age`` even if a later bound would also have removed it.

    Returns
    -------
    tuple
        Entries to delete, archives to rewrite with a trim, the closed cause of
        each deletion as ``(sequence, cause)`` pairs, and whether a configured
        budget was left to the follow-up plan.
    """
    candidates = tuple(
        entry for entry in inventory.effective if entry.sequence not in duplicated
    )
    deletes: dict[int, SegmentEntry] = {}
    causes: dict[int, str] = {}
    if budget.cutoff is not None:
        for entry in candidates:
            plan_age_step(entry, budget.cutoff, deletes)
    _attribute(deletes, causes, "age")
    trims = age_trims(candidates, budget.cutoff, deletes)
    measurable = _budgets_are_measurable(candidates, budget, deletes, duplicated)
    settled = True
    if measurable:
        # Both walks always run: the record walk composes with the byte walk's
        # decisions, so a stopped byte walk must not skip the cap.
        settled = plan_byte_steps(candidates, budget, deletes, trims)
        _attribute(deletes, causes, "bytes")
        settled = plan_record_steps(candidates, budget, deletes, trims) and settled
        _attribute(deletes, causes, "records")
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
    return (
        tuple(deletes[sequence] for sequence in sorted(deletes)),
        trimmed,
        tuple(sorted(causes.items())),
        deferred,
    )


def _attribute(
    deletes: dict[int, SegmentEntry], causes: dict[int, str], cause: str
) -> None:
    """Attribute every deletion not yet explained to the bound just applied."""
    for sequence in deletes:
        causes.setdefault(sequence, cause)


def _budgets_are_measurable(
    candidates: tuple[SegmentEntry, ...],
    budget: Budget,
    deletes: cabc.Mapping[int, SegmentEntry],
    duplicated: set[int],
) -> bool:
    """Whether a budget walk can safely choose whole deletions this pass.

    A duplicated sequence is left out of the candidates while reconciliation
    settles it, so its rows would be missing from the total the walk acts on.
    An active file whose rows could not be read leaves a byte budget without
    the committed bytes it must count; its whole-file size includes an append
    the writer may roll back, so it cannot stand in. ``_active_is_measurable``
    and ``budget_totals_are_unsettled`` report the other conditions: an entry
    whose span crosses the cutoff, or one with no trustworthy measurement. A
    later pass re-plans from the reconciled and measured history.

    Returns
    -------
    bool
        True when none of those conditions applies.
    """
    if duplicated:
        return False
    if not _active_is_measurable(budget):
        return False
    return not budget_totals_are_unsettled(candidates, budget.cutoff, deletes)


def _active_is_measurable(budget: Budget) -> bool:
    """Whether an unmeasured active file still lets the budgets be decided.

    An unmeasured file contributes only its whole size, and that size includes
    every byte of an append the writer has not committed; a failed append is
    truncated back out. Evicting closed history against those bytes would
    discard history the settled store keeps, so a configured byte bound is
    deferred until a later scan reads the file, whether or not a cutoff
    applies. The record cap needs no deferral here: a configured cap is
    refused outright by ``require_measurable_active``.

    Returns
    -------
    bool
        True when the active figures the configured byte bound needs were read.
    """
    if budget.active.has_metadata:
        return True
    return not budget.policy.has_byte_limit
