"""Planning and executing one maintenance run under the archive lock.

The command module decides which operation a firing performs and maps its
faults to a closed report. This module owns what happens once that decision is
made: it reads one consistent store version, plans from it, performs the plan,
writes the state, and measures the result. It never decides whether a run may
start, so the lock and process-status contract stay in one place.
"""

import dataclasses as dc
import typing as typ

from buildlimits import (
    outcome_archive_cleanup,
    outcome_archive_evict,
    outcome_archive_firing,
    outcome_archive_fs,
    outcome_archive_lock,
    outcome_archive_retain,
    outcome_archive_run,
    outcome_archive_settled,
    outcome_archive_state,
    outcome_segment_inventory,
    outcome_segment_removed,
    outcome_segment_state,
    outcome_segments,
)
from buildlimits.outcome_archive_budget import UnknownActiveHistoryError
from buildlimits.outcome_archive_history import SettledStore
from buildlimits.outcome_archive_plan import PlanInputs, PlanSummary, plan_run
from buildlimits.outcome_archive_report_builders import dry_report, unsettled_report
from buildlimits.outcome_archive_unmeasured import unmeasured_after_work

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits import outcome_archive_clock_guard as clock_guard
    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_archive_plan import Plan
    from buildlimits.outcome_archive_report import RunReport
    from buildlimits.outcome_segment_inventory import Inventory
    from buildlimits.outcome_segments import SegmentLayout

__all__ = ["execute_locked", "inspect"]


def inspect(context: ArchiveContext, started: dt.datetime) -> RunReport:
    """Plan and report one dry run without changing any file.

    Returns
    -------
    RunReport
        The settled dry-run report, or a retryable result when the writer kept
        moving across every reading.
    """
    plan, active, inventory, settled = _plan(context)
    if not settled:
        return unsettled_report(started, context, dry_run=True)
    return dry_report(
        _archive_run(context, plan), active, inventory, clock=context.clock
    )


def execute_locked(
    context: ArchiveContext,
    started: dt.datetime,
    reading: clock_guard.ClockReading,
) -> RunReport:
    """Plan and execute one run while its lock is held.

    Another archiver may have changed the store while this process waited for
    the lock, so the plan is built from what is on disk now rather than from a
    pre-lock inventory. A plan built from a torn pair would count neither the
    sealed copy nor its active rows, so nothing is changed and the run reports
    a retryable result instead.

    Every return records this firing's clock baseline, because the guard
    withholds age eviction only for the firing that met the jump. A failure to
    record it is reported, but does not change the store: the next firing then
    withholds age eviction once more than it needed to.

    Returns
    -------
    RunReport
        The completed run's report.
    """
    plan, _active, _inventory, settled = _plan(context)
    if not settled:
        return outcome_archive_firing.record(
            context.layout, reading, unsettled_report(started, context)
        )
    floor, report = _execute(_archive_run(context, plan))
    # The baseline is recorded before the report that describes it, so the
    # persisted report never claims a baseline write that failed afterwards.
    report = outcome_archive_firing.record(context.layout, reading, report)
    if floor is None:
        return report
    return _install_state(context, floor, report)


def _install_state(context: ArchiveContext, floor: int, report: RunReport) -> RunReport:
    """Persist the floor and the operation's report, naming a failed write.

    Returns
    -------
    RunReport
        The report, marked failed when its state could not be installed.
    """
    try:
        outcome_archive_state.write_state(context.layout, floor, report)
    except OSError:
        # The archives and their evictions are real even when the state could
        # not be replaced. The floor gets its own attempt, so completed
        # deletions are not reported as gaps later.
        outcome_archive_state.write_floor_after_failure(context.layout, floor, report)
        _clear_removal_markers(context.layout)
        return dc.replace(
            report,
            status="failed",
            reason="state_write_failed",
            maintenance=dc.replace(
                report.maintenance,
                state_written=False,
                reason="state_write_failed",
            ),
        )
    _clear_removal_markers(context.layout)
    return report


def _clear_removal_markers(layout: SegmentLayout) -> None:
    """Remove the removal markers a recorded floor now explains.

    The markers are read back from the store rather than assumed from the
    attempted floor: when both floor writes failed, nothing records the
    deletion, and the markers are the only evidence left.
    """
    recorded = outcome_segment_state.read_retention_floor(layout).sequence
    if recorded is not None:
        outcome_segment_removed.clear_covered(layout.archive_dir, recorded)


def _plan(
    context: ArchiveContext,
) -> tuple[Plan, ActiveHistory, Inventory, bool]:
    """Read one consistent store version and plan one run from it.

    The inventory and the active file are read together, retrying a seal that
    lands between them. A plan built from a torn pair would count neither the
    sealed copy nor its active rows, and the same reading supplies a dry-run
    report, so both describe one version. The active file is measured against
    the cutoff so the plan's budgets count only the rows retention keeps, while
    its whole-file figures still describe what the writer holds. A run whose
    age bound is withheld measures the whole active file, because every row the
    writer holds is then retained history.

    Returns
    -------
    tuple[Plan, ActiveHistory, Inventory, bool]
        The operations to perform, what the active file held, the inventory
        they were read with, and whether the readings describe one version of
        the store. False means no snapshot settled within the bounded
        attempts, so the caller must report an unsettled result rather than
        act on the plan.
    """
    layout = context.layout
    cutoff = None if context.suppress_age else context.policy.cutoff(context.now)
    floor = outcome_segment_state.read_retention_floor(layout)
    inventory, active, settled = outcome_archive_retain.settled_snapshot(
        layout, floor.sequence, cutoff=cutoff, on_retry=context.on_retry
    )
    return (
        plan_run(
            inventory,
            context.policy,
            PlanInputs(
                now=context.now,
                active=active,
                operation=context.operation,
                suppress_age=context.suppress_age,
            ),
        ),
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
) -> tuple[int | None, RunReport]:
    """Prepare the store, perform the plan and report what settled.

    Persisting the report is left to the locked caller: writing state is a
    separate effect from deciding what the run achieved.

    A state write that fails does not discard the report of work already done:
    the archive and its evictions are real even when ``retention.json`` could
    not be replaced. The run reports ``failed`` with ``state_write_failed``,
    so a timer retries and the next run rewrites the state from disk.

    The floor gets its own attempt before that failure is returned: a later
    run that deletes nothing learns about this run's deletions only from the
    recorded floor, so a transient failure must not turn them into permanent
    gaps. A retry that still cannot replace ``retention.json`` falls back to
    ``retention-pending.json``, which readers merge with the primary record;
    only when both refuse the write is the loss reported as a gap.

    Returns
    -------
    tuple[int | None, RunReport]
        The recorded floor, when measured, and the report.
    """
    _prepare(run.context)
    outcome = outcome_archive_run.RunOutcome()
    try:
        _perform(run, outcome)
        layout = run.context.layout
        floor = outcome_archive_state.retention_floor(
            outcome_segment_inventory.read_inventory(layout),
            outcome_segment_state.read_retention_floor(layout),
            outcome,
            outcome_segment_removed.removed_sequences(layout.archive_dir),
        )
        return floor, _report(run, outcome, floor)
    except UnknownActiveHistoryError:
        return unmeasured_after_work(run, outcome, "unreadable")
    except OSError as error:
        # A full filesystem stays retryable even though the work succeeded, so
        # the reason keeps the classification the earlier headroom check uses.
        return unmeasured_after_work(
            run, outcome, outcome_archive_evict.failure_reason(error)
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
    cutoff = (
        None if run.context.suppress_age else run.context.policy.cutoff(run.started_at)
    )
    final, final_active, snapshot_settled = outcome_archive_retain.settled_snapshot(
        layout,
        floor,
        cutoff=cutoff,
        on_retry=run.context.on_retry,
    )
    if not snapshot_settled:
        # The writer kept sealing across every reading, so the report cannot
        # bind rows to one version of the store. Report it as retryable rather
        # than recording an undercount as success.
        outcome.retention_unsettled = True
    elif outcome_archive_settled.retention_owes_work(final, final_active, run):
        # The writer can append or seal between the last retention pass and
        # this read. The report must not claim the store is within its bounds
        # when the next firing still has history to evict.
        outcome.retention_unsettled = True
    settled = SettledStore(
        inventory=final,
        active=final_active,
        retention_unsettled=outcome.retention_unsettled,
        is_settled=snapshot_settled,
    )
    return outcome_archive_settled.settled_report(run, outcome, settled)


def _prepare(context: ArchiveContext) -> None:
    """Create the directories and prove the headroom a run needs.

    An in-place sealed rewrite keeps its temporary in the sealed directory, so
    the pass first removes only the temporaries maintenance itself abandoned
    there. The writer publishes its metadata through the same directory, and
    its own temporaries must survive.
    """
    layout = context.layout
    outcome_archive_fs.ensure_private_directory(layout.archive_dir)
    outcome_archive_cleanup.remove_stale_temps(layout.archive_dir)
    outcome_archive_cleanup.remove_maintenance_files(
        layout.sealed_dir, outcome_segments.SEALED_TEMP_PREFIX
    )
    outcome_archive_lock.require_headroom(
        layout.archive_dir, context.policy.headroom_bytes
    )
    if context.operation == "retain" and layout.sealed_dir.is_dir():
        # A sealed trim writes beside the source it replaces, so that
        # filesystem needs room too. A store with no sealed directory has no
        # source to rewrite.
        outcome_archive_lock.require_headroom(
            layout.sealed_dir, context.policy.headroom_bytes
        )


def _perform(
    run: outcome_archive_run.ArchiveRun, outcome: outcome_archive_run.RunOutcome
) -> None:
    """Perform the plan and the retention passes it enables.

    Both phases run inside the caller's completed-work recovery: a retention
    pass re-plans from the store this run has just published, so a file it
    cannot read must record the deletions ``perform`` already made rather
    than escape as a report that omits them.
    """
    outcome_archive_run.perform(run, outcome)
    outcome.retention_unsettled = not outcome_archive_retain.retain(run, outcome)
