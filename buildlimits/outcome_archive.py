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
from buildlimits.outcome_archive_plan import PlanSummary, plan_run
from buildlimits.outcome_archive_report import (
    RunOptions,
    RunReport,
    RunStatus,
    dry_report,
    unfinished_report,
    unsettled_report,
)

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
    )
    report = _attempt_run(context, selected)
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
        return unfinished_report(started, "busy", "busy", clock=context.clock)
    except outcome_archive_lock.NoHeadroomError:
        return unfinished_report(
            started, "no_headroom", "no_headroom", clock=context.clock
        )
    except UnknownActiveHistoryError:
        # The record cap counts the writer's unsealed rows. A run that cannot
        # read them cannot show the store is within the cap, so it reports an
        # operational failure rather than counting the active file as empty.
        return unfinished_report(started, "failed", "unreadable", clock=context.clock)
    except OSError as error:
        # The headroom check reads free space once, before the pass, so the
        # filesystem can still fill while this run works. A full filesystem is
        # retryable; anything else needs an operator.
        reason = outcome_archive_run.failure_reason(error)
        status: RunStatus = "no_headroom" if reason == "no_headroom" else "failed"
        return unfinished_report(started, status, reason, clock=context.clock)


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
    outcome_archive_state.write_state(context.layout, floor, report)
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

    Returns
    -------
    tuple[int, RunReport]
        The retention floor the run recorded, and its report.
    """
    _prepare(run.context.layout, run.context.policy)
    outcome = _perform(run)
    floor = outcome_archive_state.retention_floor(
        outcome_segment_inventory.read_inventory(run.context.layout),
        outcome_segment_state.read_retention_floor(run.context.layout),
        outcome,
    )
    return floor, outcome_archive_report.settled_report(run, outcome, floor)


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
    outcome.retention_unsettled = not outcome_archive_retain.retain(run, outcome)
    return outcome
