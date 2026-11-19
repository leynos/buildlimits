"""Pure planning for one cache-outcome archive run.

The planner reads an inventory scanned without reading segment content, so it
never opens a file and never blocks the writer. It decides which sources to
archive, which sidecars to rebuild, which orphans to quarantine, and which
archives to trim or delete for the age and byte budgets. Retention evicts
oldest closed history first, and every estimate is conservative: a planned
trim never frees more bytes than its sidecar proves.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_archive_verify import Trim
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
        Sealed sources with no archive.
    heal : tuple[SegmentEntry, ...]
        Archives whose sidecar is missing or corrupt.
    orphans : tuple[int, ...]
        Sequences whose sidecar has no archive.
    trims : tuple[tuple[SegmentEntry, Trim], ...]
        Archives to rewrite with a prefix trim.
    deletes : tuple[SegmentEntry, ...]
        Archives to remove entirely.
    retained_bytes : int
        Conservative logical bytes that will remain after the plan runs.
    cutoff : datetime | None
        The age cutoff applied, or None when age eviction is disabled.
    """

    reconcile: tuple[tuple[SegmentEntry, SegmentEntry], ...] = ()
    archive: tuple[SegmentEntry, ...] = ()
    heal: tuple[SegmentEntry, ...] = ()
    orphans: tuple[int, ...] = ()
    trims: tuple[tuple[SegmentEntry, Trim], ...] = ()
    deletes: tuple[SegmentEntry, ...] = ()
    retained_bytes: int = 0
    cutoff: dt.datetime | None = None


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
        Archives that will be removed.
    retained_bytes : int
        Conservative logical bytes remaining after the plan.
    cutoff : datetime | None
        The age cutoff applied.
    """

    reconcile: int = 0
    archive: int = 0
    heal: int = 0
    orphan: int = 0
    trim: int = 0
    delete: int = 0
    retained_bytes: int = 0
    cutoff: dt.datetime | None = None

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
        }


@dc.dataclass(frozen=True, slots=True)
class _Budget:
    """The policy, cutoff and active bytes one plan is measured against."""

    policy: RetentionPolicy
    cutoff: dt.datetime | None
    active_bytes: int


def plan_run(
    inventory: Inventory,
    policy: RetentionPolicy,
    *,
    now: dt.datetime,
    active_bytes: int,
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
    active_bytes : int
        Logical bytes in the writer's active file.

    Returns
    -------
    Plan
        Operations in execution order plus conservative estimates.

    Examples
    --------
    An empty inventory plans nothing and retains only the active bytes.
    >>> import datetime as dt
    >>> from buildlimits.outcome_retention import RetentionPolicy
    >>> from buildlimits.outcome_segment_inventory import Inventory
    >>> now = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
    >>> plan_run(Inventory(), RetentionPolicy(), now=now, active_bytes=7).retained_bytes
    7
    """
    duplicates = tuple(
        (sealed, archived)
        for sealed in inventory.sealed
        for archived in inventory.archived
        if sealed.sequence == archived.sequence
    )
    duplicated_sequences = {sealed.sequence for sealed, _archived in duplicates}
    archive = tuple(
        entry
        for entry in inventory.sealed
        if entry.sequence not in duplicated_sequences
    )
    heal = _heal_steps(inventory, duplicated_sequences)
    budget = _Budget(
        policy=policy, cutoff=policy.cutoff(now), active_bytes=active_bytes
    )
    deletes, trims = _retention_steps(inventory, budget, duplicated_sequences)
    retained = _retained_estimate(
        inventory, active_bytes=active_bytes, deletes=deletes, trims=trims
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
    )


def _heal_steps(inventory: Inventory, duplicated: set[int]) -> tuple[SegmentEntry, ...]:
    """Return archives whose sidecar needs rebuilding.

    Returns
    -------
    tuple[SegmentEntry, ...]
        Archives with a missing or corrupt sidecar, excluding duplicates that
        reconciliation already owns.
    """
    damaged = {*inventory.missing_sidecars, *inventory.corrupt_sidecars}
    return tuple(
        entry
        for entry in inventory.archived
        if entry.sequence in damaged and entry.sequence not in duplicated
    )


def _retention_steps(
    inventory: Inventory,
    budget: _Budget,
    duplicated: set[int],
) -> tuple[tuple[SegmentEntry, ...], tuple[tuple[SegmentEntry, Trim], ...]]:
    """Decide age and byte eviction over closed archives, oldest first.

    Returns
    -------
    tuple[tuple[SegmentEntry, ...], tuple[tuple[SegmentEntry, Trim], ...]]
        Archives to delete and archives to rewrite with a trim.
    """
    candidates = tuple(
        entry for entry in inventory.archived if entry.sequence not in duplicated
    )
    deletes: dict[int, SegmentEntry] = {}
    if budget.cutoff is not None:
        for entry in candidates:
            _plan_age_step(entry, budget.cutoff, deletes)
    trims = _age_trims(candidates, budget.cutoff, deletes)
    _plan_byte_steps(candidates, budget, deletes, trims)
    trimmed = tuple(
        (entry, trims[entry.sequence])
        for entry in candidates
        if entry.sequence in trims
    )
    return tuple(deletes[sequence] for sequence in sorted(deletes)), trimmed


def _plan_age_step(
    entry: SegmentEntry, cutoff: dt.datetime, deletes: dict[int, SegmentEntry]
) -> None:
    """Record a whole-segment age deletion when every row is older."""
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
        first = entry.first_observed_at
        if first is not None and first < cutoff:
            trims[entry.sequence] = Trim(before=cutoff)
    return trims


def _plan_byte_steps(
    candidates: tuple[SegmentEntry, ...],
    budget: _Budget,
    deletes: dict[int, SegmentEntry],
    trims: dict[int, Trim],
) -> None:
    """Evict the oldest archives until the logical byte budget is met."""
    if not budget.policy.has_byte_limit:
        return
    retained = _retained_from_candidates(candidates, budget, deletes, trims)
    excess = retained - budget.policy.max_bytes
    if excess <= 0:
        return
    for entry in _live(candidates, deletes):
        if excess <= 0:
            # A whole-archive deletion may have met the budget exactly. The
            # next archive then needs no rewrite, and trimming it to its own
            # size would churn the file without dropping a row.
            return
        size = _planned_size(entry, trims.get(entry.sequence))
        if size <= excess:
            deletes[entry.sequence] = entry
            excess -= size
            continue
        existing = trims.get(entry.sequence, Trim())
        trims[entry.sequence] = dc.replace(
            existing, allowed_bytes=size - excess, source_bytes=size
        )
        return


def _live(
    candidates: tuple[SegmentEntry, ...], deletes: dict[int, SegmentEntry]
) -> cabc.Iterator[SegmentEntry]:
    """Yield the candidates no step has deleted yet.

    The delete set is consulted as each entry is yielded, so a planner that
    adds to it while walking still sees its own decisions.

    Returns
    -------
    Iterator[SegmentEntry]
        The candidates still planned to survive, in sequence order.
    """
    return (entry for entry in candidates if entry.sequence not in deletes)


def _retained_from_candidates(
    candidates: tuple[SegmentEntry, ...],
    budget: _Budget,
    deletes: dict[int, SegmentEntry],
    trims: dict[int, Trim],
) -> int:
    """Estimate retained bytes from the closed archives and active file.

    Returns
    -------
    int
        Active bytes plus every candidate that is not deleted, with planned
        trims applied.
    """
    return budget.active_bytes + sum(
        _planned_size(entry, trims.get(entry.sequence))
        for entry in _live(candidates, deletes)
    )


def _planned_size(entry: SegmentEntry, trim: Trim | None) -> int:
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
        Active bytes plus every sealed source plus every archive that is not
        deleted, with planned trims applied.
    """
    deleted = {entry.sequence for entry in deletes}
    trim_map = {entry.sequence: trim for entry, trim in trims}
    retained = active_bytes
    for entry in inventory.effective:
        if entry.state == "sealed":
            retained += entry.logical_bytes
        elif entry.sequence not in deleted:
            retained += _planned_size(entry, trim_map.get(entry.sequence))
    return retained
