"""Archive closed cache-outcome history into verified XZ segments.

A run plans from a content-free inventory, takes the archive lock, compresses
and verifies one sealed source at a time, then applies the age and logical
byte budget by trimming or deleting the oldest closed history. The active file
is never opened for writing: only the writer seals it. Every failure is
reported as a closed reason and an exit status a systemd timer can act on.
"""

import dataclasses as dc
import datetime as dt
import typing as typ

from buildlimits import (
    outcome_archive_active,
    outcome_archive_fs,
    outcome_archive_lock,
    outcome_archive_run,
    outcome_segment_inventory,
    outcome_segment_state,
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
    Maintenance,
    RunOptions,
    RunReport,
    RunStatus,
)
from buildlimits.outcome_retention_history import retained_history
from buildlimits.outcome_retention_report import RetentionLimits
from buildlimits.utc_text import optional_utc_text, utc_now, utc_text

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_plan import Plan
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_retention_report import ActiveHistory
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
    context = ArchiveContext(
        layout=layout, policy=policy, preset=selected.preset, now=started
    )
    try:
        if selected.dry_run:
            # A dry run mutates nothing, so it may plan without the lock.
            plan, active = _plan(layout, policy, started)
            return _dry_report(_archive_run(context, plan), active)
        with outcome_archive_lock.ArchiveLock(layout.lock_file):
            # Another archiver may have changed the store while this process
            # waited for the lock, so the plan is built from what is on disk
            # now rather than from a pre-lock inventory.
            plan, active = _plan(layout, policy, started)
            return _execute(_archive_run(context, plan), active)
    except outcome_archive_lock.ArchiveBusyError:
        return _unfinished(started, "busy", "busy", policy)
    except outcome_archive_lock.NoHeadroomError:
        return _unfinished(started, "no_headroom", "no_headroom", policy)
    except OSError:
        return _unfinished(started, "failed", "unreadable", policy)


def _plan(
    layout: SegmentLayout, policy: RetentionPolicy, now: dt.datetime
) -> tuple[Plan, ActiveHistory]:
    """Read the store as it is and plan one run from it.

    The active file is read once, through a bounded reader, so its size drives
    the plan and its rows describe the retained history without a second pass.

    Returns
    -------
    tuple[Plan, ActiveHistory]
        The operations to perform and the active file's measured state.
    """
    floor = outcome_segment_state.read_retention_floor(layout)
    inventory = outcome_segment_inventory.read_inventory(layout, retention_floor=floor)
    active = outcome_archive_active.read_active_history(layout.active)
    plan = plan_run(inventory, policy, now=now, active_bytes=active.logical_bytes)
    return plan, active


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


def _execute(run: outcome_archive_run.ArchiveRun, active: ActiveHistory) -> RunReport:
    """Take the archive directories, perform the plan and write state.

    A state write that fails does not discard the report of work already done:
    the archive and its evictions are real even when ``retention.json`` could
    not be replaced. The run then reports ``failed`` with
    ``state_write_failed``, so a timer retries and the next run rewrites the
    state from what is on disk.

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
    floor = _retention_floor(
        outcome_segment_inventory.read_inventory(layout),
        outcome_segment_state.read_retention_floor(layout),
        outcome,
    )
    final = outcome_segment_inventory.read_inventory(layout, retention_floor=floor)
    measured = retained_history(final, active)
    records, observed_from, observed_until = _observed(final)
    report = RunReport(
        status="corrupt" if outcome.failures else "ok",
        started_at=run.started_at,
        finished_at=utc_now(),
        active_bytes=active.logical_bytes,
        retained_bytes=measured.logical_bytes,
        retained_records=records,
        observed_from=observed_from,
        observed_until=observed_until,
        budget_bytes=run.context.policy.max_bytes,
        cutoff=run.plan.cutoff,
        plan=run.summary,
        actions=tuple(outcome.actions),
        failures=tuple(outcome.failures),
        gaps=final.gaps,
        gaps_truncated=final.gaps_truncated,
        missing_sidecars=final.missing_sidecars,
        corrupt_sidecars=final.corrupt_sidecars,
        orphan_sidecars=final.orphan_sidecars,
        limits=RetentionLimits.of(run.context.policy),
        retained=measured,
    )
    try:
        _write_state(layout, floor, report)
    except OSError:
        return dc.replace(
            report,
            status="failed",
            reason="state_write_failed",
            maintenance=Maintenance(state_written=False, reason="state_write_failed"),
        )
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
            "limits": None if report.limits is None else report.limits.as_dict(),
            "retained": None if report.retained is None else report.retained.as_dict(),
            "updated_at": utc_text(report.finished_at),
        },
    )
    outcome_archive_fs.atomic_write_json(layout.last_run_file, report.as_dict())


def _retention_floor(
    inventory: outcome_segment_inventory.Inventory,
    previous: int | None,
    outcome: outcome_archive_run.RunOutcome,
) -> int:
    """Return the lowest sequence this history intentionally starts at.

    The floor records what retention removed, not what happens to be absent.
    Deriving it from the surviving sequences would raise it past a sequence
    that went missing for another reason, and the next scan would then stop
    reporting that sequence as a gap. It therefore advances only past a
    deleted prefix, and never falls below the first sequence this store ever
    had, so an empty prefix is not reported as missing history.

    Parameters
    ----------
    inventory : Inventory
        The store as it stands after the run.
    previous : int | None
        The floor the last successful run recorded.
    outcome : RunOutcome
        The actions this run performed.

    Returns
    -------
    int
        The retention floor to persist.
    """
    present = [entry.sequence for entry in inventory.effective]
    baseline = min(present, default=0) if previous is None else previous
    deleted = [
        action.sequence for action in outcome.actions if action.action == "deleted"
    ]
    return max([baseline, *(sequence + 1 for sequence in deleted)])


def _observed(
    inventory: outcome_segment_inventory.Inventory,
) -> tuple[int, dt.datetime | None, dt.datetime | None]:
    """Summarize the closed history metadata describes.

    Returns
    -------
    tuple[int, datetime | None, datetime | None]
        Retained record count, earliest and latest observation. Entries
        without trustworthy metadata contribute nothing rather than guessing.
    """
    described = [
        entry for entry in inventory.effective if entry.record_count is not None
    ]
    records = sum(entry.record_count or 0 for entry in described)
    first = [entry.first_observed_at for entry in described if entry.first_observed_at]
    last = [entry.last_observed_at for entry in described if entry.last_observed_at]
    return records, min(first, default=None), max(last, default=None)


def _dry_report(
    run: outcome_archive_run.ArchiveRun, active: ActiveHistory
) -> RunReport:
    """Build the report a dry run returns without changing any file.

    ``retained_bytes`` is the plan's conservative estimate, because a dry run
    is asked what the bound would be after the work. ``retained`` describes
    the state on disk now, which a caller needs to tell the two apart.

    Returns
    -------
    RunReport
        A successful dry-run report carrying the plan and current history.
    """
    layout = run.context.layout
    floor = outcome_segment_state.read_retention_floor(layout)
    inventory = outcome_segment_inventory.read_inventory(layout, retention_floor=floor)
    records, observed_from, observed_until = _observed(inventory)
    return RunReport(
        status="ok",
        started_at=run.started_at,
        finished_at=utc_now(),
        dry_run=True,
        active_bytes=active.logical_bytes,
        retained_bytes=run.plan.retained_bytes,
        retained_records=records,
        observed_from=observed_from,
        observed_until=observed_until,
        budget_bytes=run.context.policy.max_bytes,
        cutoff=run.plan.cutoff,
        plan=run.summary,
        gaps=inventory.gaps,
        gaps_truncated=inventory.gaps_truncated,
        missing_sidecars=inventory.missing_sidecars,
        corrupt_sidecars=inventory.corrupt_sidecars,
        orphan_sidecars=inventory.orphan_sidecars,
        limits=RetentionLimits.of(run.context.policy),
        retained=retained_history(inventory, active),
    )


def _unfinished(
    started: dt.datetime,
    status: RunStatus,
    reason: str,
    policy: RetentionPolicy,
) -> RunReport:
    """Build a report for a run that could not start or complete.

    The effective limits are reported even when no run happened, so a caller
    that only reads the failure document still learns the policy in force.

    Returns
    -------
    RunReport
        The closed failure report.
    """
    return RunReport(
        status=status,
        started_at=started,
        finished_at=utc_now(),
        limits=RetentionLimits.of(policy),
        reason=reason,
    )
# weave: run 'weave explain buildlimits/outcome_archive.py' for per-hunk detail, 'weave check' to verify your resolution
