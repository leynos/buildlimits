"""Executing one archive plan, one closed operation at a time.

Every per-segment operation is wrapped here so one unreadable file becomes a
recorded failure instead of ending the run. The order is fixed: reconcile first
so a half-published pair is settled before anything is archived again, then
archive, heal, quarantine orphans, trim and finally delete.
"""

import dataclasses as dc
import typing as typ

from buildlimits import (
    outcome_archive_files,
    outcome_archive_quarantine,
    outcome_segment_inventory,
)
from buildlimits.outcome_archive_context import ActionResult, ArchiveContext
from buildlimits.outcome_archive_plan import PlanSummary, plan_run
from buildlimits.outcome_archive_verify import SegmentFaultError, Trim

if typ.TYPE_CHECKING:
    import datetime as dt
    from pathlib import Path

    from buildlimits.outcome_archive_plan import Plan
    from buildlimits.outcome_segment_inventory import SegmentEntry

__all__ = ["ArchiveRun", "RunOutcome", "active_bytes", "perform", "retain"]


@dc.dataclass(frozen=True, slots=True)
class ArchiveRun:
    """The immutable inputs one execution pass shares.

    Attributes
    ----------
    context : ArchiveContext
        The run's layout, policy, preset and moment.
    plan : Plan
        The operations the run intends to perform.
    summary : PlanSummary
        The bounded summary of that plan for the report.
    started_at : datetime
        The moment the run started.
    """

    context: ArchiveContext
    plan: Plan
    summary: PlanSummary
    started_at: dt.datetime


@dc.dataclass(slots=True)
class RunOutcome:
    """The mutations one run accumulates while it performs its plan."""

    actions: list[ActionResult] = dc.field(default_factory=list)
    failures: list[tuple[int, str]] = dc.field(default_factory=list)


def perform(run: ArchiveRun, outcome: RunOutcome) -> None:
    """Execute every planned operation in a fixed order."""
    for sealed, archived in run.plan.reconcile:
        _reconcile_one(run, sealed, archived, outcome)
    for entry in run.plan.archive:
        _archive_one(run, entry, outcome)
    for entry in run.plan.heal:
        _heal_one(run, entry, outcome)
    outcome.actions.extend(
        outcome_archive_quarantine.remove_orphan_sidecar(run.context, sequence)
        for sequence in run.plan.orphans
    )
    for entry, trim in run.plan.trims:
        _trim_one(run, entry, trim, outcome)
    for entry in run.plan.deletes:
        _delete_one(run, entry, outcome)


def retain(run: ArchiveRun, outcome: RunOutcome) -> None:
    """Apply age and byte retention to history this run just archived.

    The first plan saw the newly published archives as sealed sources, so it
    could not evict them. Re-planning over the settled store keeps the byte
    budget true after one successful run instead of leaving the excess for the
    next timer firing.
    """
    if not (run.plan.archive or run.plan.reconcile):
        return
    layout = run.context.layout
    inventory = outcome_segment_inventory.read_inventory(layout)
    follow_up = plan_run(
        inventory,
        run.context.policy,
        now=run.context.now,
        active_bytes=active_bytes(layout.active),
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


def active_bytes(path: Path) -> int:
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


def _reconcile_one(
    run: ArchiveRun,
    sealed: SegmentEntry,
    archived: SegmentEntry,
    outcome: RunOutcome,
) -> None:
    """Settle one already-published pair, quarantining a bad source."""
    try:
        outcome.actions.append(
            outcome_archive_files.reconcile_duplicate(run.context, sealed, archived)
        )
    except OSError:
        outcome.failures.append((sealed.sequence, "unreadable"))


def _archive_one(run: ArchiveRun, entry: SegmentEntry, outcome: RunOutcome) -> None:
    """Archive one sealed source, quarantining it when it is invalid."""
    try:
        outcome.actions.append(outcome_archive_files.archive_source(run.context, entry))
    except SegmentFaultError as fault:
        outcome.actions.append(
            outcome_archive_quarantine.quarantine_source(
                run.context, entry, fault.reason
            )
        )
        outcome.failures.append((entry.sequence, fault.reason))
    except OSError:
        outcome.failures.append((entry.sequence, "unreadable"))


def _heal_one(run: ArchiveRun, entry: SegmentEntry, outcome: RunOutcome) -> None:
    """Rebuild one sidecar, quarantining an archive that will not verify."""
    try:
        outcome.actions.append(outcome_archive_files.repair_sidecar(run.context, entry))
    except SegmentFaultError as fault:
        outcome.actions.append(
            outcome_archive_quarantine.quarantine_archive(
                run.context, entry, fault.reason
            )
        )
        outcome.failures.append((entry.sequence, fault.reason))
    except OSError:
        outcome.failures.append((entry.sequence, "unreadable"))


def _trim_one(
    run: ArchiveRun,
    entry: SegmentEntry,
    trim: Trim,
    outcome: RunOutcome,
) -> None:
    """Apply one planned trim, quarantining an archive that will not verify."""
    try:
        outcome.actions.append(
            outcome_archive_files.recompress_archive(run.context, entry, trim)
        )
    except SegmentFaultError as fault:
        outcome.actions.append(
            outcome_archive_quarantine.quarantine_archive(
                run.context, entry, fault.reason
            )
        )
        outcome.failures.append((entry.sequence, fault.reason))
    except OSError:
        outcome.failures.append((entry.sequence, "unreadable"))


def _delete_one(run: ArchiveRun, entry: SegmentEntry, outcome: RunOutcome) -> None:
    """Remove one expired archive."""
    try:
        outcome_archive_files.delete_archive(run.context, entry)
    except OSError:
        outcome.failures.append((entry.sequence, "unreadable"))
        return
    outcome.actions.append(ActionResult("deleted", entry.sequence))
