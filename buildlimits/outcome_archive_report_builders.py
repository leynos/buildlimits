"""The builders that classify an archive run's outcome.

Each builder produces the bounded report shape from ``outcome_archive_report``:
a dry run, a store that never settled, a run that could not start or complete,
and a run whose work succeeded but whose result could not be measured. The
clock is supplied at the boundary rather than read from a module default, so a
caller decides what instant a finish time reports.
"""

import typing as typ

from buildlimits.outcome_archive_report import Maintenance, RunReport, Unfinished
from buildlimits.outcome_archive_report_fields import settled_fields
from buildlimits.outcome_retention_report import RetentionLimits

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_archive_run import ArchiveRun, RunOutcome
    from buildlimits.outcome_segment_inventory import Inventory

__all__ = [
    "after_work_failure",
    "dry_report",
    "unfinished_report",
    "unsettled_report",
]


def dry_report(
    run: ArchiveRun,
    active: ActiveHistory,
    inventory: Inventory,
    *,
    clock: cabc.Callable[[], dt.datetime],
) -> RunReport:
    """Build the report a dry run returns without changing any file.

    The inventory and the active history are the settled pair the plan was
    built from, so the report and the plan describe one version of the store;
    reading the inventory again could count a seal between the two. An active
    file that could not be counted makes the retained totals a lower bound, so
    the inspection reports ``corrupt`` rather than claiming the history it
    could not read is empty. No state file is installed, and the report says so.

    Returns
    -------
    RunReport
        A dry-run report carrying the plan and the history it planned against.
    """
    return RunReport(
        status=(
            "ok"
            if active.has_metadata
            else (
                "unsettled"
                if active.reason in {"unreadable", "unsettled"}
                else "corrupt"
            )
        ),
        operation=run.context.operation,
        clock_skew=run.context.clock_skew,
        started_at=run.started_at,
        finished_at=clock(),
        dry_run=True,
        **settled_fields(run, inventory, active),
        maintenance=Maintenance(state_written=False),
        # The reason the active file could not be counted is the reason the
        # inspection reports ``corrupt``; leaving it out would name the status
        # without the closed fault behind it.
        reason=None if active.has_metadata else active.reason,
    )


def unsettled_report(
    started_at: dt.datetime,
    context: ArchiveContext,
    *,
    dry_run: bool = False,
) -> RunReport:
    """Build a retryable report for a store that never held still.

    The writer can keep sealing across every bounded reading, so no inventory
    and active file describe one version of the store. Reporting a plan or a
    count from any of those pairs would undercount history, so nothing is
    planned, nothing is changed, and the timer retries. The context still
    supplies the configured limits and the clock the finish time is read from,
    so a retryable report never loses the policy that will be retried.

    Returns
    -------
    RunReport
        An ``unsettled`` report with no plan.
    """
    limits = RetentionLimits.of(context.policy)
    return RunReport(
        status="unsettled",
        operation=context.operation,
        clock_skew=context.clock_skew,
        started_at=started_at,
        finished_at=context.clock(),
        dry_run=dry_run,
        budget_bytes=limits.max_bytes,
        limits=limits,
        maintenance=Maintenance(state_written=False, reason="unsettled"),
    )


def unfinished_report(
    started_at: dt.datetime,
    fault: Unfinished,
    context: ArchiveContext,
    *,
    dry_run: bool = False,
) -> RunReport:
    """Build a report for a run that could not start or complete.

    The effective limits are reported even when no run happened, so a caller
    that only reads the failure document still learns the policy in force. The
    dry-run marker is carried too: an inspection that could not start changed
    no file, and its report must not read as a run that did.

    Returns
    -------
    RunReport
        The closed failure report.
    """
    limits = RetentionLimits.of(context.policy)
    return RunReport(
        status=fault.status,
        operation=context.operation,
        clock_skew=context.clock_skew,
        started_at=started_at,
        finished_at=context.clock(),
        dry_run=dry_run,
        budget_bytes=limits.max_bytes,
        limits=limits,
        maintenance=Maintenance(state_written=False, reason=fault.reason),
        reason=fault.reason,
    )


def after_work_failure(
    run: ArchiveRun,
    outcome: RunOutcome,
    reason: str,
) -> RunReport:
    """Report a run whose result could not be measured after it did the work.

    The archives and evictions are already published, so the actions and
    failures are reported as they happened. The retained totals are unknown,
    and no state file was installed; a retry recomputes both from the disk.

    Parameters
    ----------
    run : ArchiveRun
        The plan and context this run executed; its context supplies the
        clock the finish time is read from.
    outcome : RunOutcome
        The actions and failures this run performed.
    reason : str
        The closed reason the result could not be measured.

    Returns
    -------
    RunReport
        A report that still records the work the run completed. A full
        filesystem stays retryable; every other reason is a failure.
    """
    return RunReport(
        status="no_headroom" if reason == "no_headroom" else "failed",
        operation=run.context.operation,
        clock_skew=run.context.clock_skew,
        started_at=run.started_at,
        finished_at=run.context.clock(),
        budget_bytes=run.context.policy.max_bytes,
        cutoff=run.plan.cutoff,
        plan=run.summary,
        actions=tuple(outcome.actions),
        failures=tuple(outcome.failures),
        limits=RetentionLimits.of(run.context.policy),
        maintenance=Maintenance(state_written=False, reason=reason),
        reason=reason,
    )
