"""Settling the history one maintenance run leaves behind.

The writer never takes the archive lock, so it can seal a source after the run
has planned and before the follow-up passes look again. Each pass re-reads the
store, performs the repair, archive, reconcile, heal and orphan work it
discovers, then applies the retention the settled inventory plans. A
retention-only run performs the same passes without an archive step, so what a
seal leaves behind is measured and trimmed rather than compressed. The passes
are bounded; a run that still finds work reports an unsettled result so the
next firing retries.
"""

import typing as typ

from buildlimits import outcome_archive_pending, outcome_segment_inventory
from buildlimits.outcome_archive_active import active_history
from buildlimits.outcome_archive_history import ActiveHistory
from buildlimits.outcome_archive_plan import PlanInputs, PlanSummary, plan_run
from buildlimits.outcome_archive_run import (
    ArchiveRun,
    RunOutcome,
    perform,
    quarantined_history,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_archive_pending import AppliedTrims
    from buildlimits.outcome_archive_plan import Plan
    from buildlimits.outcome_segment_inventory import Inventory
    from buildlimits.outcome_segments import SegmentLayout

__all__ = ["retain", "settled_snapshot"]

# Follow-up planning passes one run may make after its plan is performed. The
# first settles the sources this run archived, including a source the writer
# sealed mid-run, the second decides a count trim that had to wait for a time
# or byte trim to be applied, and the third applies a count trim the second
# pass discovered. Work a pass cannot finish is left for the next timer firing
# rather than retried without bound, and a check made after the last pass
# distinguishes "settled" from "ran out of passes".
#
# A pass also spends one iteration when the writer seals between the inventory
# scan and the active-file read: the plan would count neither copy, so it is
# abandoned and rebuilt from a fresh pair of reads.
_MAX_RETAIN_PASSES = 3

# Final-report readings retried when the writer moves a source out of the
# active file between the inventory and active scans. Three readings cover a
# seal on each attempt without making the report wait indefinitely.
_SNAPSHOT_ATTEMPTS = 3


class _InventoryChangedError(Exception):
    """The writer sealed a source while a follow-up plan read the store.

    A seal moves rows out of the active file and into a source that a stale
    inventory does not list. Planning from that pair would count neither copy
    and could leave the store over its bound while reporting success, so the
    pass is abandoned and retried from a fresh pair of reads.
    """


def retain(run: ArchiveRun, outcome: RunOutcome) -> bool:
    """Apply retention to the store as it stands after the performed plan.

    The first plan saw the newly published archives as sealed sources, so it
    could not evict them. Re-planning over the settled store keeps the budgets
    true after one successful run instead of leaving the excess for the next
    timer firing. A run that only healed sidecars re-plans too, because the
    first plan had to fall back to each archive's physical size where a
    sidecar was missing or corrupt, which does not describe the logical bytes
    the archive holds.

    A source the writer seals during the run is discovered by the same pass
    and archived before the budget is planned over it, so a record cap can
    still be met in one firing. A trim is not repeated against the archive
    version it already rewrote, but the same trim against an archive the
    active file has since outgrown is a new decision and is applied. An
    archive, reconcile, heal or orphan step that failed is left for the next
    firing, and an eviction failure leaves that entry and every newer eviction
    to the next firing too, so the oldest-first order the budget planned is
    never inverted.

    Parameters
    ----------
    run : ArchiveRun
        The run whose performed plan and context the follow-up uses.
    outcome : RunOutcome
        The actions and failures accumulated so far.

    Returns
    -------
    bool
        Whether the bounded passes settled the store. False means work the run
        discovered is still pending, or a pass could not confirm what it saw,
        so the caller reports a retryable result rather than success.
    """
    applied = outcome_archive_pending.applied_trims(run, outcome)
    removed = outcome_archive_pending.removed(outcome)
    for attempt in range(_MAX_RETAIN_PASSES):
        try:
            pending = _pending_work(run, outcome, applied, removed)
        except _InventoryChangedError:
            # The store moved while this pass read it. Spend another bounded
            # pass on a fresh pair of reads rather than acting on a plan that
            # counted neither the sealed source nor its active rows.
            run.context.on_retry("retain", attempt + 1, "inventory_changed")
            continue
        if pending is None:
            return True
        failures = len(outcome.failures)
        first_action = len(outcome.actions)
        perform(_follow_up_run(run, pending), outcome)
        if len(outcome.failures) > failures:
            # The failure is already reported, and it outranks a deferred
            # status: the next firing retries what this one could not change.
            return True
        if quarantined_history(outcome, first_action):
            # The pass returned before its eviction steps, so none of this
            # plan's trims or deletions ran. Crediting them would make the
            # next pass filter the still-present entries as already handled
            # and report the store settled with its budget unmet. The plan is
            # rebuilt from the store as the quarantine left it instead.
            continue
        applied.update(outcome_archive_pending.measured(pending.trims))
        removed.update(entry.sequence for entry in pending.deletes)
    try:
        # The last pass may have finished the last operation, so a check made
        # without mutating anything distinguishes a settled store from one
        # that merely ran out of passes.
        return _pending_work(run, outcome, applied, removed) is None
    except _InventoryChangedError:
        return False


def _pending_work(
    run: ArchiveRun,
    outcome: RunOutcome,
    applied: AppliedTrims,
    removed: set[int],
) -> Plan | None:
    """Plan the current store and narrow it to work this run has not done.

    Returns
    -------
    Plan or None
        None when the store plans no further work.
    """
    follow_up = _replan(run)
    narrowed = outcome_archive_pending.narrow(
        follow_up,
        outcome_archive_pending.Done(
            failed={sequence for sequence, _reason in outcome.failures},
            blocked=outcome_archive_pending.failed_eviction(run, outcome),
            applied=applied,
            removed=removed,
        ),
    )
    if outcome_archive_pending.has_work(narrowed):
        return narrowed
    # A deferred budget with nothing left to perform cannot settle in this
    # run: no action repairs it. Returning the empty plan keeps the run
    # retryable instead of claiming success over a budget it could not
    # measure, while a store with nothing pending reports settled.
    return narrowed if follow_up.retention_deferred else None


def _replan(run: ArchiveRun) -> Plan:
    """Plan the store as it stands now, including any concurrent seal.

    The writer never takes the archive lock, so it can seal between the
    inventory scan and the active-file read. Such a seal moves rows out of the
    active file and into a source the first scan does not list, so the pair
    would count neither copy. Reading the inventory again after the active
    file detects that move; a seal after the second read is harmless, because
    the plan then describes the store as it stood when the active file was
    read.

    Returns
    -------
    Plan
        The operations the current inventory and active file call for.

    Raises
    ------
    _InventoryChangedError
        If a seal changed the inventory across the active-file read.
    """
    layout = run.context.layout
    inventory = outcome_segment_inventory.read_inventory(layout)
    active = active_history(
        layout.active,
        cutoff=run.context.policy.cutoff(run.context.now),
        on_retry=run.context.on_retry,
    )
    if outcome_segment_inventory.read_inventory(layout) != inventory:
        msg = "a source was sealed while the follow-up plan read the store"
        raise _InventoryChangedError(msg)
    return plan_run(
        inventory,
        run.context.policy,
        PlanInputs(
            now=run.context.now,
            active=active,
            operation=run.context.operation,
            suppress_age=run.context.suppress_age,
        ),
    )


def settled_snapshot(
    layout: SegmentLayout,
    floor: int | None,
    *,
    cutoff: dt.datetime | None = None,
    on_retry: cabc.Callable[[str, int, str], None] | None = None,
) -> tuple[Inventory, ActiveHistory, bool]:
    """Read one inventory and active version for a plan or a report.

    A seal between the inventory scan and the active-file read moves rows out
    of the active file into a source the scan does not list, so the pair would
    describe neither copy. Reading the inventory again after the active file
    detects that move; the snapshot is accepted only when the two scans agree.
    A seal after the second scan is harmless, because the active read and the
    inventory then describe the store as it stood at that read.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    floor : int | None
        The retention floor the inventory scan reports gaps from, or None when
        the store has recorded none.
    cutoff : datetime | None
        The age cutoff to measure the active rows against, or None to count
        the whole file. The returned history carries both figures either way.
    on_retry : Callable[[str, int, str], None] | None
        Receives each retried read's operation, attempt and category.

    Returns
    -------
    tuple[Inventory, ActiveHistory, bool]
        The last inventory and active readings, and whether they describe one
        version of the store. A writer that keeps sealing across every attempt
        leaves the caller to report an unsettled result so the timer retries.
    """
    inventory = outcome_segment_inventory.read_inventory(layout, retention_floor=floor)
    active = ActiveHistory()
    for attempt in range(_SNAPSHOT_ATTEMPTS):
        active = active_history(layout.active, cutoff=cutoff, on_retry=on_retry)
        after = outcome_segment_inventory.read_inventory(layout, retention_floor=floor)
        if after == inventory:
            return inventory, active, True
        inventory = after
        if on_retry is not None:
            on_retry("snapshot", attempt + 1, "inventory_changed")
    return inventory, active, False


def _follow_up_run(run: ArchiveRun, plan: Plan) -> ArchiveRun:
    """Bind one follow-up plan to the context of the performed run.

    Returns
    -------
    ArchiveRun
        The follow-up plan as an executable run.
    """
    return ArchiveRun(
        context=run.context,
        plan=plan,
        summary=PlanSummary.of(plan),
        started_at=run.started_at,
    )
