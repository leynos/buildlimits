"""Settling the history an archive run leaves behind.

The writer never takes the archive lock, so it can seal a source after the run
has planned and before the follow-up passes look again. Each pass re-reads the
store, performs the archive, reconcile, heal and orphan work it discovers, and
then applies the retention the settled inventory plans. The passes are
bounded; a run that still finds work after them reports an unsettled result so
the next timer firing retries instead of claiming success.
"""

import dataclasses as dc
import typing as typ

from buildlimits import outcome_segment_inventory
from buildlimits.outcome_archive_active import active_history
from buildlimits.outcome_archive_plan import PlanSummary, plan_run
from buildlimits.outcome_archive_run import ArchiveRun, RunOutcome, perform

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_plan import Plan
    from buildlimits.outcome_archive_trim import Trim

__all__ = ["retain"]

# Follow-up planning passes one run may make after its plan is performed. The
# first settles the sources this run archived, including a source the writer
# sealed mid-run, the second decides a count trim that had to wait for a time
# or byte trim to be applied, and the third confirms the settled store plans no
# further work. Work a pass cannot finish is left for the next timer firing
# rather than retried without bound.
_MAX_RETAIN_PASSES = 3


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
    still be met in one firing. A trim or deletion this run already applied is
    not repeated. An archive, reconcile, heal or orphan step that failed is
    left for the next firing, and an eviction failure leaves that entry and
    every newer eviction to the next firing too, so the oldest-first order the
    budget planned is never inverted.

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
        discovered is still pending, so the caller reports a retryable result
        rather than success.
    """
    applied = _applied_trims(run, outcome)
    removed = _removed(outcome)
    for _pass in range(_MAX_RETAIN_PASSES):
        pending = _pending_work(run, outcome, applied, removed)
        if pending is None:
            return True
        failures = len(outcome.failures)
        perform(_follow_up_run(run, pending), outcome)
        if len(outcome.failures) > failures:
            # The failure is already reported, and it outranks a deferred
            # status: the next firing retries what this one could not change.
            return True
        applied.update((entry.sequence, trim) for entry, trim in pending.trims)
        removed.update(entry.sequence for entry in pending.deletes)
    return False


def _pending_work(
    run: ArchiveRun,
    outcome: RunOutcome,
    applied: set[tuple[int, Trim]],
    removed: set[int],
) -> Plan | None:
    """Plan the current store and narrow it to work this run has not done.

    Returns
    -------
    Plan or None
        None when the store plans no further work.
    """
    follow_up = _replan(run)
    failed = {sequence for sequence, _reason in outcome.failures}
    blocked = _failed_eviction(run, outcome)
    archive = tuple(
        entry for entry in follow_up.archive if entry.sequence not in failed
    )
    reconcile = tuple(
        pair for pair in follow_up.reconcile if pair[0].sequence not in failed
    )
    heal = tuple(entry for entry in follow_up.heal if entry.sequence not in failed)
    orphans = tuple(
        sequence for sequence in follow_up.orphans if sequence not in failed
    )
    # An eviction step that failed is not retried. The eviction order is
    # oldest-first, so every step at or after that sequence is left for the
    # next firing: trimming a newer archive would discard rows from history
    # that survives while the entry the budget asked for is still present.
    trims = tuple(
        (entry, trim)
        for entry, trim in follow_up.trims
        if _evictable(entry.sequence, removed, blocked)
        and entry.path.exists()
        and (entry.sequence, trim) not in applied
    )
    deletes = tuple(
        entry
        for entry in follow_up.deletes
        if _evictable(entry.sequence, removed, blocked) and entry.path.exists()
    )
    if not any((archive, reconcile, heal, orphans, trims, deletes)):
        return None
    return dc.replace(
        follow_up,
        archive=archive,
        reconcile=reconcile,
        heal=heal,
        orphans=orphans,
        trims=trims,
        deletes=deletes,
    )


def _evictable(sequence: int, removed: set[int], blocked: int | None) -> bool:
    """Whether an eviction step for ``sequence`` may run in this pass.

    Returns
    -------
    bool
        False when the entry is already removed, or when an eviction at or
        before this sequence failed and its newer steps must wait.
    """
    return sequence not in removed and (blocked is None or sequence < blocked)


def _failed_eviction(run: ArchiveRun, outcome: RunOutcome) -> int | None:
    """Return the oldest sequence whose planned eviction failed, if any.

    Returns
    -------
    int or None
        The barrier sequence, or None when no eviction step failed.
    """
    failed = {sequence for sequence, _reason in outcome.failures}
    planned = [entry.sequence for entry, _trim in run.plan.trims]
    planned.extend(entry.sequence for entry in run.plan.deletes)
    return min((sequence for sequence in planned if sequence in failed), default=None)


def _replan(run: ArchiveRun) -> Plan:
    """Plan the store as it stands now, including any concurrent seal.

    Returns
    -------
    Plan
        The operations the current inventory and active file call for.
    """
    layout = run.context.layout
    inventory = outcome_segment_inventory.read_inventory(layout)
    return plan_run(
        inventory,
        run.context.policy,
        now=run.context.now,
        active=active_history(layout.active),
    )


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


def _applied_trims(run: ArchiveRun, outcome: RunOutcome) -> set[tuple[int, Trim]]:
    """Return the trims the performed plan applied, so they are not repeated.

    Returns
    -------
    set[tuple[int, Trim]]
        The sequence and trim value of every applied trim.
    """
    trimmed = {
        action.sequence for action in outcome.actions if action.action == "trimmed"
    }
    return {
        (entry.sequence, trim)
        for entry, trim in run.plan.trims
        if entry.sequence in trimmed
    }


def _removed(outcome: RunOutcome) -> set[int]:
    """Return the sequences this run removed, which no later pass can reuse.

    Only a deletion removes an entry. A quarantined *source* leaves its
    archive in place, and that archive is still eligible for the budget a
    follow-up plan recomputes.

    Returns
    -------
    set[int]
        The sequences removed whole.
    """
    return {action.sequence for action in outcome.actions if action.action == "deleted"}
