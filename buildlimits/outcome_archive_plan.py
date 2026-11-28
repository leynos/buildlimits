"""Pure planning for one cache-outcome maintenance run.

The planner reads an inventory scanned without reading segment content, so it
never opens a file and never blocks the writer. It decides which sources to
archive, which sidecars or sealed metadata to rebuild, which orphans to
quarantine, and which closed history to trim or delete for the age and byte
budgets. Retention evicts oldest closed history first, whether it is a sealed
source or an archive: a budget that counts a sealed source's rows must be able
to remove them, or the plan would delete newer archives while older history
survived. Every estimate is conservative: a planned trim never frees more bytes
than its sidecar proves.

An ``archive`` run compresses every sealed source it keeps and then applies
retention. A ``retain`` run applies the same bounds to the same history without
compressing anything: a sealed straddler is rewritten in place, an archive is
recompressed, and a whole entry is removed whichever state it is in.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_archive_budget import Budget
from buildlimits.outcome_archive_retention_plan import retention_steps
from buildlimits.outcome_archive_sizes import retained_estimate
from buildlimits.outcome_archive_summary import PlanSummary as PlanSummary

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_archive_context import Operation
    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_archive_trim import Trim
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segment_inventory import Inventory, SegmentEntry

__all__ = ["Plan", "PlanInputs", "PlanSummary", "plan_run"]


@dc.dataclass(frozen=True, slots=True)
class PlanInputs:
    """The store reading and operation one plan is built from.

    The reading and the mode travel together because every planner helper
    needs both, and a helper that reads one but not the other cannot decide a
    bound the run is allowed to apply.

    Attributes
    ----------
    now : datetime
        The caller-supplied UTC wall time.
    active : ActiveHistory
        What the writer's active file holds, measured against the age cutoff
        when one applies so only the rows retention keeps count.
    operation : Operation
        ``archive`` compresses sealed sources and applies retention;
        ``retain`` applies the same retention without compressing anything.
    suppress_age : bool
        Whether the age bound is withheld because this run's clock reading was
        judged suspect. The byte and record bounds still apply.
    """

    now: dt.datetime
    active: ActiveHistory
    operation: Operation = "archive"
    suppress_age: bool = False

    @property
    def can_trim_sealed(self) -> bool:
        """Whether a sealed source may be rewritten in place.

        Returns
        -------
        bool
            True for a retention-only plan.
        """
        return self.operation == "retain"

    def cutoff(self, policy: RetentionPolicy) -> dt.datetime | None:
        """Return the age cutoff this plan applies, if any.

        Returns
        -------
        datetime | None
            The cutoff, or None when age eviction is disabled or withheld.
        """
        return None if self.suppress_age else policy.cutoff(self.now)


@dc.dataclass(frozen=True, slots=True)
class Plan:
    """The bounded set of operations one run should perform.

    Attributes
    ----------
    reconcile : tuple[tuple[SegmentEntry, SegmentEntry], ...]
        Sealed sources whose archive was already published.
    archive : tuple[SegmentEntry, ...]
        Sealed sources with no archive that retention does not evict. Always
        empty for a retention-only plan.
    heal : tuple[SegmentEntry, ...]
        Archives whose sidecar is missing or corrupt.
    sealed_repairs : tuple[SegmentEntry, ...]
        Sealed sources whose metadata must be re-derived before any age, byte
        or record decision can be trusted.
    stale_sealed_markers : tuple[int, ...]
        Sequences whose in-place rewrite marker has no sealed source left.
    orphans : tuple[int, ...]
        Sequences whose sidecar has no archive.
    trims : tuple[tuple[SegmentEntry, Trim], ...]
        Entries to rewrite with a prefix trim. An archive is recompressed; a
        sealed source is rewritten in place.
    deletes : tuple[SegmentEntry, ...]
        Archives and sealed sources to remove entirely.
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
    sealed_repairs: tuple[SegmentEntry, ...] = ()
    stale_sealed_markers: tuple[int, ...] = ()
    orphans: tuple[int, ...] = ()
    trims: tuple[tuple[SegmentEntry, Trim], ...] = ()
    deletes: tuple[SegmentEntry, ...] = ()
    eviction_causes: tuple[tuple[int, str], ...] = ()
    retained_bytes: int = 0
    cutoff: dt.datetime | None = None
    retention_deferred: bool = False


def plan_run(inventory: Inventory, policy: RetentionPolicy, inputs: PlanInputs) -> Plan:
    """Decide every operation one maintenance run should perform.

    Parameters
    ----------
    inventory : Inventory
        The read-only closed-history inventory.
    policy : RetentionPolicy
        The effective retention policy.
    inputs : PlanInputs
        The store reading and the operation the plan is built for.

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
    ...     Inventory(),
    ...     RetentionPolicy(),
    ...     PlanInputs(now=now, active=ActiveHistory(bytes=7)),
    ... ).retained_bytes
    7
    """
    duplicates = _duplicates(inventory)
    duplicated = {sealed.sequence for sealed, _archived in duplicates}
    heal = _heal_steps(inventory, duplicated)
    budget = Budget(
        policy=policy,
        cutoff=inputs.cutoff(policy),
        active=inputs.active,
        can_trim_sealed=inputs.can_trim_sealed,
    )
    retention = retention_steps(inventory, budget, duplicated)
    archive = _archive_steps(inventory, duplicated, retention.deletes, inputs=inputs)
    repairs = _sealed_repair_steps(
        inventory,
        archived={entry.sequence for entry in archive} | duplicated,
        deleted={entry.sequence for entry in retention.deletes},
    )
    return Plan(
        reconcile=duplicates,
        archive=archive,
        heal=heal,
        sealed_repairs=repairs,
        stale_sealed_markers=_stale_markers(inventory),
        orphans=inventory.orphan_sidecars,
        trims=retention.trims,
        deletes=retention.deletes,
        eviction_causes=retention.causes,
        retained_bytes=retained_estimate(
            inventory,
            active_bytes=inputs.active.budget_bytes,
            deletes=retention.deletes,
            trims=retention.trims,
        ),
        cutoff=budget.cutoff,
        retention_deferred=retention.deferred,
    )


def _sealed_repair_steps(
    inventory: Inventory,
    *,
    archived: set[int],
    deleted: set[int],
) -> tuple[SegmentEntry, ...]:
    """Return sealed sources whose metadata must be re-derived first.

    A source is unmeasurable when its metadata is missing or unusable, or when
    an unfinished in-place rewrite makes the metadata untrusted. Nothing can
    then be decided about its age, bytes or rows, and a retention-only run
    cannot fall back on compressing it. A source this run archives is measured
    by that pass, and a source this run deletes needs no description.

    Returns
    -------
    tuple[SegmentEntry, ...]
        The sources to repair, in sequence order.
    """
    return tuple(
        entry
        for entry in inventory.sealed
        if entry.sequence not in archived
        and entry.sequence not in deleted
        and (entry.sequence in inventory.sealed_replacing or not entry.has_metadata)
    )


def _stale_markers(inventory: Inventory) -> tuple[int, ...]:
    """Return sealed rewrite markers whose source is already gone.

    A removal that finished before its marker was cleared leaves a marker with
    no file. It names no history, exists only because a process stopped between
    two steps, and must be cleared so the next inventory does not report
    unfinished work for ever.

    Returns
    -------
    tuple[int, ...]
        The sequences whose marker should be removed.
    """
    present = {entry.sequence for entry in inventory.sealed}
    return tuple(
        sequence for sequence in inventory.sealed_replacing if sequence not in present
    )


def _archive_steps(
    inventory: Inventory,
    duplicated: set[int],
    deletes: tuple[SegmentEntry, ...],
    *,
    inputs: PlanInputs,
) -> tuple[SegmentEntry, ...]:
    """Return the sealed sources this run still has to compress.

    A source retention evicts is removed whole instead of being compressed and
    then deleted: compression would only defer the same removal. A duplicated
    source is settled by reconciliation instead. A retention-only run plans no
    archival at all, so its sources stay sealed until a bound rewrites them in
    place.

    Returns
    -------
    tuple[SegmentEntry, ...]
        The sources to archive.
    """
    if inputs.operation != "archive":
        return ()
    evicted = {entry.sequence for entry in deletes}
    return tuple(
        entry
        for entry in inventory.sealed
        if entry.sequence not in duplicated and entry.sequence not in evicted
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
