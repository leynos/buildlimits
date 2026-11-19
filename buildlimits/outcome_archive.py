"""Archive closed cache-outcome history into verified XZ segments.

A run plans from a content-free inventory, takes the archive lock, compresses
and verifies one sealed source at a time, then applies the age and logical
byte budget by trimming or deleting the oldest closed history. The active file
is never opened for writing: only the writer seals it. Every failure is
reported as a closed reason and an exit status a systemd timer can act on.
"""

import datetime as dt
import typing as typ

from buildlimits import (
    outcome_archive_fs,
    outcome_archive_lock,
    outcome_archive_run,
    outcome_segment_inventory,
)
from buildlimits.outcome_archive_context import ArchiveContext
from buildlimits.outcome_archive_plan import PlanSummary, plan_run
from buildlimits.outcome_archive_report import (
    ARCHIVE_SCHEMA,
    EXIT_CORRUPT,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_TEMPFAIL,
    EXIT_USAGE,
    RunOptions,
    RunReport,
    RunStatus,
)
from buildlimits.utc_text import optional_utc_text, utc_text

if typ.TYPE_CHECKING:
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segments import SegmentLayout

_STATE_SCHEMA = 1

__all__ = [
    "ARCHIVE_SCHEMA",
    "EXIT_CORRUPT",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_TEMPFAIL",
    "EXIT_USAGE",
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
    now: dt.datetime | None = None,
) -> RunReport:
    """Report the store's history and the plan a run would execute.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    policy : RetentionPolicy
        The effective retention policy.
    now : datetime | None
        The UTC wall time to use, or None to read the clock once.

    Returns
    -------
    RunReport
        A dry-run report; no file is created or changed.
    """
    return run_archive(layout, policy, RunOptions(now=now, dry_run=True))


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
    started = dt.datetime.now(dt.UTC) if selected.now is None else selected.now
    try:
        floor = outcome_segment_inventory.read_retention_floor(layout)
        inventory = outcome_segment_inventory.read_inventory(
            layout, retention_floor=floor
        )
        active = outcome_archive_run.active_bytes(layout.active)
    except OSError:
        return _unfinished(started, "failed", "unreadable")
    plan = plan_run(inventory, policy, now=started, active_bytes=active)
    context = ArchiveContext(
        layout=layout, policy=policy, preset=selected.preset, now=started
    )
    run = outcome_archive_run.ArchiveRun(
        context=context,
        plan=plan,
        summary=PlanSummary.of(plan),
        started_at=started,
    )
    if selected.dry_run:
        return _dry_report(run, active)
    try:
        with outcome_archive_lock.ArchiveLock(layout.lock_file):
            return _execute(run, active)
    except outcome_archive_lock.ArchiveBusyError:
        return _unfinished(started, "busy", "busy")
    except outcome_archive_lock.NoHeadroomError:
        return _unfinished(started, "no_headroom", "no_headroom")
    except OSError:
        return _unfinished(started, "failed", "unreadable")


def _execute(run: outcome_archive_run.ArchiveRun, active: int) -> RunReport:
    """Take the archive directories, perform the plan and write state.

    Returns
    -------
    RunReport
        The completed run's report.
    """
    layout = run.context.layout
    outcome_archive_fs.ensure_private_directory(layout.archive_dir)
    outcome_archive_fs.remove_stale_temps(layout.archive_dir)
    outcome_archive_lock.require_headroom(
        layout.archive_dir, run.context.policy.headroom_bytes
    )
    outcome = outcome_archive_run.RunOutcome()
    outcome_archive_run.perform(run, outcome)
    outcome_archive_run.retain(run, outcome)
    floor = _retention_floor(outcome_segment_inventory.read_inventory(layout))
    final = outcome_segment_inventory.read_inventory(layout, retention_floor=floor)
    retained = outcome_archive_run.active_bytes(layout.active) + final.sealed_bytes
    retained += final.archived_logical_bytes
    report = RunReport(
        status="corrupt" if outcome.failures else "ok",
        started_at=run.started_at,
        finished_at=_now(),
        active_bytes=active,
        retained_bytes=retained,
        budget_bytes=run.context.policy.max_bytes,
        cutoff=run.plan.cutoff,
        plan=run.summary,
        actions=tuple(outcome.actions),
        failures=tuple(outcome.failures),
        gaps=final.gaps,
        missing_sidecars=final.missing_sidecars,
        corrupt_sidecars=final.corrupt_sidecars,
        orphan_sidecars=final.orphan_sidecars,
    )
    _write_state(layout, floor, report)
    return report


def _write_state(layout: SegmentLayout, floor: int, report: RunReport) -> None:
    """Persist the retention floor and the run report durably."""
    outcome_archive_fs.atomic_write_json(
        layout.retention_file,
        {
            "schema": _STATE_SCHEMA,
            "floor_sequence": floor,
            "retained_logical_bytes": report.retained_bytes,
            "budget_bytes": report.budget_bytes,
            "cutoff": optional_utc_text(report.cutoff),
            "updated_at": utc_text(report.finished_at),
        },
    )
    outcome_archive_fs.atomic_write_json(layout.last_run_file, report.as_dict())


def _retention_floor(inventory: outcome_segment_inventory.Inventory) -> int:
    """Return the lowest retained closed sequence for the retention state.

    Returns
    -------
    int
        The lowest effective sequence, or zero when no closed history remains.
    """
    sequences = [entry.sequence for entry in inventory.effective]
    return min(sequences, default=0)


def _dry_report(run: outcome_archive_run.ArchiveRun, active: int) -> RunReport:
    """Build the report a dry run returns without changing any file.

    Returns
    -------
    RunReport
        A successful dry-run report carrying the plan.
    """
    layout = run.context.layout
    floor = outcome_segment_inventory.read_retention_floor(layout)
    inventory = outcome_segment_inventory.read_inventory(layout, retention_floor=floor)
    return RunReport(
        status="ok",
        started_at=run.started_at,
        finished_at=_now(),
        dry_run=True,
        active_bytes=active,
        retained_bytes=run.plan.retained_bytes,
        budget_bytes=run.context.policy.max_bytes,
        cutoff=run.plan.cutoff,
        plan=run.summary,
        gaps=inventory.gaps,
        missing_sidecars=inventory.missing_sidecars,
        corrupt_sidecars=inventory.corrupt_sidecars,
        orphan_sidecars=inventory.orphan_sidecars,
    )


def _unfinished(started: dt.datetime, status: RunStatus, reason: str) -> RunReport:
    """Build a report for a run that could not start or complete.

    Returns
    -------
    RunReport
        The closed failure report.
    """
    return RunReport(
        status=status,
        started_at=started,
        finished_at=_now(),
        reason=reason,
    )


def _now() -> dt.datetime:
    """Read the wall clock once for a report timestamp.

    Returns
    -------
    datetime
        The current UTC moment.
    """
    return dt.datetime.now(dt.UTC)
