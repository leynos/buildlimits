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

from buildlimits import outcome_archive_files, outcome_archive_fs, outcome_segments
from buildlimits.outcome_archive_files import ActionResult, ArchiveContext
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
from buildlimits.outcome_archive_verify import SegmentFaultError, Trim
from buildlimits.utc_text import optional_utc_text, utc_text

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_archive_plan import Plan
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segments import SegmentEntry, SegmentLayout

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


@dc.dataclass(frozen=True, slots=True)
class _Run:
    """The immutable inputs one execution pass shares."""

    context: ArchiveContext
    plan: Plan
    summary: PlanSummary
    started_at: dt.datetime


@dc.dataclass(slots=True)
class _Outcome:
    """The mutations one run accumulates while it performs its plan."""

    actions: list[ActionResult] = dc.field(default_factory=list)
    failures: list[tuple[int, str]] = dc.field(default_factory=list)


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
        floor = outcome_segments.read_retention_floor(layout)
        inventory = outcome_segments.read_inventory(layout, retention_floor=floor)
        active = _active_bytes(layout.active)
    except OSError:
        return _unfinished(started, "failed", "unreadable")
    plan = plan_run(inventory, policy, now=started, active_bytes=active)
    context = ArchiveContext(
        layout=layout, policy=policy, preset=selected.preset, now=started
    )
    run = _Run(
        context=context,
        plan=plan,
        summary=PlanSummary.of(plan),
        started_at=started,
    )
    if selected.dry_run:
        return _dry_report(run, active)
    try:
        with outcome_archive_fs.ArchiveLock(layout.lock_file):
            return _execute(run, active)
    except outcome_archive_fs.ArchiveBusyError:
        return _unfinished(started, "busy", "busy")
    except outcome_archive_fs.NoHeadroomError:
        return _unfinished(started, "no_headroom", "no_headroom")
    except OSError:
        return _unfinished(started, "failed", "unreadable")


def _execute(run: _Run, active: int) -> RunReport:
    """Take the archive directories, perform the plan and write state.

    Returns
    -------
    RunReport
        The completed run's report.
    """
    layout = run.context.layout
    outcome_archive_fs.ensure_private_directory(layout.archive_dir)
    outcome_archive_fs.remove_stale_temps(layout.archive_dir)
    outcome_archive_fs.require_headroom(
        layout.archive_dir, run.context.policy.headroom_bytes
    )
    outcome = _Outcome()
    _perform(run, outcome)
    _retain(run, outcome)
    floor = _retention_floor(outcome_segments.read_inventory(layout))
    final = outcome_segments.read_inventory(layout, retention_floor=floor)
    retained = _active_bytes(layout.active) + final.sealed_bytes
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


def _perform(run: _Run, outcome: _Outcome) -> None:
    """Execute every planned operation in a fixed order."""
    for sealed, archived in run.plan.reconcile:
        _reconcile_one(run, sealed, archived, outcome)
    for entry in run.plan.archive:
        _archive_one(run, entry, outcome)
    for entry in run.plan.heal:
        _heal_one(run, entry, outcome)
    outcome.actions.extend(
        outcome_archive_files.remove_orphan_sidecar(run.context, sequence)
        for sequence in run.plan.orphans
    )
    for entry, trim in run.plan.trims:
        _trim_one(run, entry, trim, outcome)
    for entry in run.plan.deletes:
        _delete_one(run, entry, outcome)


def _retain(run: _Run, outcome: _Outcome) -> None:
    """Apply age and byte retention to history this run just archived.

    The first plan saw the newly published archives as sealed sources, so it
    could not evict them. Re-planning over the settled store keeps the byte
    budget true after one successful run instead of leaving the excess for the
    next timer firing.
    """
    if not (run.plan.archive or run.plan.reconcile):
        return
    layout = run.context.layout
    inventory = outcome_segments.read_inventory(layout)
    follow_up = plan_run(
        inventory,
        run.context.policy,
        now=run.context.now,
        active_bytes=_active_bytes(layout.active),
    )
    settled = {
        action.sequence
        for action in outcome.actions
        if action.action in {"deleted", "quarantined"}
    }
    settled.update(sequence for sequence, _reason in outcome.failures)
    for entry, trim in follow_up.trims:
        if entry.sequence not in settled:
            _trim_one(run, entry, trim, outcome)
    for entry in follow_up.deletes:
        if entry.sequence not in settled:
            _delete_one(run, entry, outcome)


def _reconcile_one(
    run: _Run,
    sealed: SegmentEntry,
    archived: SegmentEntry,
    outcome: _Outcome,
) -> None:
    """Settle one already-published pair, quarantining a bad source."""
    try:
        outcome.actions.append(
            outcome_archive_files.reconcile_duplicate(run.context, sealed, archived)
        )
    except OSError:
        outcome.failures.append((sealed.sequence, "unreadable"))


def _archive_one(run: _Run, entry: SegmentEntry, outcome: _Outcome) -> None:
    """Archive one sealed source, quarantining it when it is invalid."""
    try:
        outcome.actions.append(outcome_archive_files.archive_source(run.context, entry))
    except SegmentFaultError as fault:
        outcome.actions.append(
            outcome_archive_files.quarantine_source(run.context, entry, fault.reason)
        )
        outcome.failures.append((entry.sequence, fault.reason))
    except OSError:
        outcome.failures.append((entry.sequence, "unreadable"))


def _heal_one(run: _Run, entry: SegmentEntry, outcome: _Outcome) -> None:
    """Rebuild one sidecar, quarantining an archive that will not verify."""
    try:
        outcome.actions.append(outcome_archive_files.repair_sidecar(run.context, entry))
    except SegmentFaultError as fault:
        outcome.actions.append(
            outcome_archive_files.quarantine_archive(run.context, entry, fault.reason)
        )
        outcome.failures.append((entry.sequence, fault.reason))
    except OSError:
        outcome.failures.append((entry.sequence, "unreadable"))


def _trim_one(
    run: _Run,
    entry: SegmentEntry,
    trim: Trim,
    outcome: _Outcome,
) -> None:
    """Apply one planned trim, quarantining an archive that will not verify."""
    try:
        outcome.actions.append(
            outcome_archive_files.recompress_archive(run.context, entry, trim)
        )
    except SegmentFaultError as fault:
        outcome.actions.append(
            outcome_archive_files.quarantine_archive(run.context, entry, fault.reason)
        )
        outcome.failures.append((entry.sequence, fault.reason))
    except OSError:
        outcome.failures.append((entry.sequence, "unreadable"))


def _delete_one(run: _Run, entry: SegmentEntry, outcome: _Outcome) -> None:
    """Remove one expired archive."""
    try:
        outcome_archive_files.delete_archive(run.context, entry)
    except OSError:
        outcome.failures.append((entry.sequence, "unreadable"))
        return
    outcome.actions.append(ActionResult("deleted", entry.sequence))


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


def _retention_floor(inventory: outcome_segments.Inventory) -> int:
    """Return the lowest retained closed sequence for the retention state.

    Returns
    -------
    int
        The lowest effective sequence, or zero when no closed history remains.
    """
    sequences = [entry.sequence for entry in inventory.effective]
    return min(sequences, default=0)


def _dry_report(run: _Run, active: int) -> RunReport:
    """Build the report a dry run returns without changing any file.

    Returns
    -------
    RunReport
        A successful dry-run report carrying the plan.
    """
    layout = run.context.layout
    floor = outcome_segments.read_retention_floor(layout)
    inventory = outcome_segments.read_inventory(layout, retention_floor=floor)
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


def _active_bytes(path: Path) -> int:
    """Return the active file's logical size, or zero when it is absent.

    Returns
    -------
    int
        The regular file's size in bytes.
    """
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return 0
    return metadata.st_size


def _now() -> dt.datetime:
    """Read the wall clock once for a report timestamp.

    Returns
    -------
    datetime
        The current UTC moment.
    """
    return dt.datetime.now(dt.UTC)
