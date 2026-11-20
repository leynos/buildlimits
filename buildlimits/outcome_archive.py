"""Archive closed cache-outcome history into verified XZ segments.

A run plans from a content-free inventory, takes the archive lock, compresses
and verifies one sealed source at a time, then applies the age and logical
byte budget by trimming or deleting the oldest closed history. The active file
is never opened for writing: only the writer seals it. Every failure is
reported as a closed reason and an exit status a systemd timer can act on.
"""

import dataclasses as dc
import typing as typ

from buildlimits import (
    outcome_archive_events,
    outcome_archive_fs,
    outcome_archive_lock,
    outcome_archive_report,
    outcome_archive_retain,
    outcome_archive_run,
    outcome_archive_state,
    outcome_segment_inventory,
    outcome_segment_state,
)
from buildlimits.outcome_archive_budget import UnknownActiveHistoryError
from buildlimits.outcome_archive_context import ArchiveContext
from buildlimits.outcome_archive_history import SettledStore
from buildlimits.outcome_archive_plan import PlanSummary, plan_run
from buildlimits.outcome_archive_quarantine import fresh_quarantine_token
from buildlimits.outcome_archive_report import (
    Maintenance,
    RunOptions,
    RunReport,
    RunStatus,
    dry_report,
    unfinished_report,
    unsettled_report,
)
from buildlimits.outcome_retention_report import RetentionLimits

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_archive_plan import Plan
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segment_inventory import Inventory
    from buildlimits.outcome_segments import SegmentLayout

__all__ = [
    "RunOptions",
    "RunReport",
    "RunStatus",
    "inspect_store",
    "run_archive",
]


def inspect_store(
    layout: SegmentLayout,
    policy: RetentionPolicy,
    *,
    options: RunOptions | None = None,
) -> RunReport:
    """Report the store's history and the plan a run would execute.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    policy : RetentionPolicy
        The effective retention policy.
    options : RunOptions | None
        The clocks, retry observer and metrics port the inspection uses. The
        dry-run flag is forced on, because an inspection never changes a file.

    Returns
    -------
    RunReport
        A dry-run report; no file is created or changed.
    """
    selected = RunOptions() if options is None else options
    return run_archive(layout, policy, dc.replace(selected, dry_run=True))


def run_archive(
    layout: SegmentLayout,
    policy: RetentionPolicy,
    options: RunOptions | None = None,
) -> RunReport:
    """Plan and optionally execute one archive run.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    policy : RetentionPolicy
        The effective retention policy.
    options : RunOptions | None
        The caller's preset, time and dry-run choice.

    Returns
    -------
    RunReport
        What the run did, or would do, and its bounded exit status.
    """
    selected = RunOptions() if options is None else options
    started = selected.clock() if selected.now is None else selected.now
    context = ArchiveContext(
        layout=layout,
        policy=policy,
        preset=selected.preset,
        now=started,
        clock=selected.clock,
        on_retry=selected.on_retry,
        quarantine_token=fresh_quarantine_token,
    )
    report = _attempt_run(context, selected)
    if not selected.dry_run:
        # An inspection is a query: it reads the store and reports, but it
        # does not publish an execution event.
        outcome_archive_events.publish_run(selected.metrics, report)
    return report


def _attempt_run(context: ArchiveContext, selected: RunOptions) -> RunReport:
    """Run or inspect the store, mapping every fault to a closed report.

    Returns
    -------
    RunReport
        The run's report, whatever the store did.
    """
    started = context.now
    try:
        if selected.dry_run:
            # A dry run mutates nothing, so it may plan without the lock.
            return _inspect(context, context.policy, started)
        with outcome_archive_lock.ArchiveLock(context.layout.lock_file):
            return _execute_locked(context, context.policy, started)
    except outcome_archive_lock.ArchiveBusyError:
        return unfinished_report(
            started,
            "busy",
            "busy",
            clock=context.clock,
            limits=RetentionLimits.of(context.policy),
            dry_run=selected.dry_run,
        )
    except outcome_archive_lock.NoHeadroomError:
        return unfinished_report(
            started,
            "no_headroom",
            "no_headroom",
            clock=context.clock,
            limits=RetentionLimits.of(context.policy),
            dry_run=selected.dry_run,
        )
    except UnknownActiveHistoryError:
        # The record cap counts the writer's unsealed rows. A run that cannot
        # read them cannot show the store is within the cap, so it reports an
        # operational failure rather than counting the active file as empty.
        return unfinished_report(
            started,
            "failed",
            "unreadable",
            clock=context.clock,
            limits=RetentionLimits.of(context.policy),
            dry_run=selected.dry_run,
        )
    except OSError as error:
        # The headroom check reads free space once, before the pass, so the
        # filesystem can still fill while this run works. A full filesystem is
        # retryable; anything else needs an operator.
        reason = outcome_archive_run.failure_reason(error)
        status: RunStatus = "no_headroom" if reason == "no_headroom" else "failed"
        return unfinished_report(
            started,
            status,
            reason,
            clock=context.clock,
            limits=RetentionLimits.of(context.policy),
            dry_run=selected.dry_run,
        )


def _inspect(
    context: ArchiveContext, policy: RetentionPolicy, started: dt.datetime
) -> RunReport:
    """Plan and report one dry run without changing any file.

    Returns
    -------
    RunReport
        The settled dry-run report, or a retryable result when the writer kept
        moving across every reading.
    """
    plan, active, inventory, settled = _plan(
        context.layout, policy, started, on_retry=context.on_retry
    )
    if not settled:
        return unsettled_report(started, dry_run=True, clock=context.clock)
    return dry_report(_archive_run(context, plan), active, inventory)


def _execute_locked(
    context: ArchiveContext, policy: RetentionPolicy, started: dt.datetime
) -> RunReport:
    """Plan and execute one run while its lock is held.

    Another archiver may have changed the store while this process waited for
    the lock, so the plan is built from what is on disk now rather than from a
    pre-lock inventory. A plan built from a torn pair would count neither the
    sealed copy nor its active rows, so nothing is changed and the run reports
    a retryable result instead.

    Returns
    -------
    RunReport
        The completed run's report.
    """
    plan, _active, _inventory, settled = _plan(
        context.layout, policy, started, on_retry=context.on_retry
    )
    if not settled:
        return unsettled_report(started, clock=context.clock)
    floor, report = _execute(_archive_run(context, plan))
    if floor is None:
        return report
    try:
        outcome_archive_state.write_state(context.layout, floor, report)
    except OSError:
        # The archive and its evictions are real even when the state could not
        # be replaced. The floor gets its own attempt, so completed deletions
        # are not reported as gaps later.
        outcome_archive_state.write_floor_after_failure(
            context.layout, floor, report
        )
        return dc.replace(
            report,
            status="failed",
            reason="state_write_failed",
            maintenance=Maintenance(
                state_written=False, reason="state_write_failed"
            ),
        )
    return report


def _plan(
    layout: SegmentLayout,
    policy: RetentionPolicy,
    now: dt.datetime,
    *,
    on_retry: cabc.Callable[[str, int, str], None] | None = None,
) -> tuple[Plan, ActiveHistory, Inventory, bool]:
    """Read one consistent store version and plan one run from it.

    The inventory and the active file are read together, retrying a seal that
    lands between them. A plan built from a torn pair would count neither the
    sealed copy nor its active rows, and the same reading supplies a dry-run
    report, so both describe one version. The active file is measured against
    the cutoff so the plan's budgets count only the rows retention keeps, while
    its whole-file figures still describe what the writer holds.

    Returns
    -------
    tuple[Plan, ActiveHistory, Inventory, bool]
        The operations to perform, what the active file held, the inventory
        they were read with, and whether the readings describe one version of
        the store. False means no snapshot settled within the bounded
        attempts, so the caller must report an unsettled result rather than
        act on the plan.
    """
    floor = outcome_segment_state.read_retention_floor(layout)
    inventory, active, settled = outcome_archive_retain.settled_snapshot(
        layout, floor.sequence, cutoff=policy.cutoff(now), on_retry=on_retry
    )
    return (
        plan_run(inventory, policy, now=now, active=active),
        active,
        inventory,
        settled,
    )


def _archive_run(context: ArchiveContext, plan: Plan) -> outcome_archive_run.ArchiveRun:
    """Bind one plan to the context its execution shares.

    Returns
    -------
    ArchiveRun
        The immutable inputs of one execution pass.
    """
    return outcome_archive_run.ArchiveRun(
        context=context,
        plan=plan,
        summary=PlanSummary.of(plan),
        started_at=context.now,
    )


def _execute(
    run: outcome_archive_run.ArchiveRun,
) -> tuple[int, RunReport]:
    """Prepare the store, perform the plan and report what settled.

    Persisting the report is left to the locked caller: writing state is a
    separate effect from deciding what the run achieved.

    A run whose active file cannot be counted, or was replaced while it was
    read, is reported as ``corrupt`` even when every closed segment succeeded:
    the retained total is then a lower bound, and a timer that recorded ``ok``
    would hide history it cannot see.

    A state write that fails does not discard the report of work already done:
    the archive and its evictions are real even when ``retention.json`` could
    not be replaced. The run then reports ``failed`` with
    ``state_write_failed``, so a timer retries and the next run rewrites the
    state from what is on disk.

    The floor gets its own attempt before that failure is returned. A later
    run that deletes nothing can only learn about this run's deletions from
    the recorded floor, so a transient failure of the full write must not
    turn them into permanent gaps. A post-run read that fails takes the same
    path: the deletions are already real, so the floor is written from their
    actions without needing another inventory read. A retry that still cannot
    replace ``retention.json`` falls back to ``retention-pending.json``, which
    every reader merges with the primary record; only when both records refuse
    the write is the loss reported as a gap.

    The retained totals come from ``_measure``, which reads each side of the
    store once and detects a seal racing those reads. A replacement in the
    window leaves the active contribution unmeasured, which marks the totals
    incomplete rather than counting the sealed rows twice.

    Returns
    -------
    tuple[int, RunReport]
        The retention floor the run recorded, and its report.
    """
    _prepare(run.context.layout, run.context.policy)
    outcome = _perform(run)
    try:
        floor = outcome_archive_state.retention_floor(
            outcome_segment_inventory.read_inventory(run.context.layout),
            outcome_segment_state.read_retention_floor(run.context.layout),
            outcome,
        )
        return floor, _report(run, outcome, floor)
    except OSError:
        # The archives and evictions are already published. The deletions
        # are the only record the floor has, so it is written from the
        # actions before the unmeasurable result is reported.
        outcome_archive_state.write_deleted_floor(
            run.context.layout,
            outcome_segment_state.read_retention_floor(
                run.context.layout
            ).sequence,
            outcome_archive_run.deleted_sequences(outcome.actions),
        )
        return (
            None,
            outcome_archive_report.after_work_failure(run, outcome, "unreadable"),
        )


def _report(
    run: outcome_archive_run.ArchiveRun,
    outcome: outcome_archive_run.RunOutcome,
    floor: int,
) -> RunReport:
    """Measure the settled store and build the report that describes it.

    The read and the retention decision stay here, in the orchestration that
    owns the store; the report builder receives their results as values.

    Returns
    -------
    RunReport
        The completed run's report, before it is persisted.
    """
    layout = run.context.layout
    final, final_active, snapshot_settled = outcome_archive_retain.settled_snapshot(
        layout,
        floor,
        cutoff=run.context.policy.cutoff(run.started_at),
        on_retry=run.context.on_retry,
    )
    if not snapshot_settled:
        # The writer kept sealing across every reading, so the report cannot
        # bind rows to one version of the store. Report it as retryable rather
        # than recording an undercount as success.
        outcome.retention_unsettled = True
    elif _retention_owes_work(final, final_active, run):
        # The writer can append or seal between the last retention pass and
        # this read. The report must not claim the store is within its bounds
        # when the next firing still has history to evict.
        outcome.retention_unsettled = True
    settled = SettledStore(
        inventory=final,
        active=final_active,
        retention_unsettled=outcome.retention_unsettled,
    )
    return outcome_archive_report.settled_report(run, outcome, settled)


def _retention_owes_work(
    inventory: Inventory, active: ActiveHistory, run: outcome_archive_run.ArchiveRun
) -> bool:
    """Whether the final snapshot still calls for retention work.

    The writer never takes the archive lock, so it can append or seal between
    the last retention pass and this read. A report that claimed success over
    that store would tell the timer the budget is met while the next firing
    still has to trim or delete. Only retention decides this: a source sealed
    just before the read is archival work the next firing picks up, and the
    report already counts its bytes.

    Returns
    -------
    bool
        True when the plan still evicts history or defers a budget.
    """
    pending = plan_run(inventory, run.context.policy, now=run.started_at, active=active)
    return bool(pending.trims or pending.deletes or pending.retention_deferred)


def _prepare(layout: SegmentLayout, policy: RetentionPolicy) -> None:
    """Create the archive directories and prove the headroom they need."""
    outcome_archive_fs.ensure_private_directory(layout.archive_dir)
    outcome_archive_fs.remove_stale_temps(layout.archive_dir)
    outcome_archive_lock.require_headroom(layout.archive_dir, policy.headroom_bytes)


def _perform(run: outcome_archive_run.ArchiveRun) -> outcome_archive_run.RunOutcome:
    """Perform the plan and the retention passes it enables.

    Returns
    -------
    RunOutcome
        The actions and failures the run recorded, including whether its
        retention passes still owe work.
    """
    outcome = outcome_archive_run.RunOutcome()
    outcome_archive_run.perform(run, outcome)
    try:
        # The retention pass re-plans from the store this run has just
        # published, so it can fail to read what ``perform`` already wrote.
        outcome.retention_unsettled = not outcome_archive_retain.retain(run, outcome)
    except OSError:
        # The work is already published. Keep its record and let the caller's
        # measurement report the store as unreadable.
        outcome.retention_unsettled = True
    return outcome
