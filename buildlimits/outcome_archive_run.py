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
    import collections.abc as cabc
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
    for sequence in run.plan.orphans:
        _quarantine_orphan(run, sequence, outcome)
    for entry, trim in run.plan.trims:
        _trim_one(run, entry, trim, outcome)
    for entry in run.plan.deletes:
        _delete_one(run, entry, outcome)


def retain(run: ArchiveRun, outcome: RunOutcome) -> None:
    """Apply age and byte retention to history this run just archived.

    The first plan saw the newly published archives as sealed sources, so it
    could not evict them. Re-planning over the settled store keeps the byte
    budget true after one successful run instead of leaving the excess for the
    next timer firing. A run that only healed sidecars re-plans too, because
    the first plan had to fall back to each archive's physical size where a
    sidecar was missing or corrupt, which understates the logical bytes.
    """
    if not (run.plan.archive or run.plan.reconcile or run.plan.heal):
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


def _quarantine_orphan(run: ArchiveRun, sequence: int, outcome: RunOutcome) -> None:
    """Quarantine one orphaned sidecar, recording a filesystem failure.

    An orphan says nothing about whether the rest of the plan is safe to run,
    so a failure here is recorded and the run continues to trim and delete.
    """
    try:
        outcome.actions.append(
            outcome_archive_quarantine.remove_orphan_sidecar(run.context, sequence)
        )
    except OSError:
        outcome.failures.append((sequence, "unreadable"))


def _quarantining(
    outcome: RunOutcome,
    sequence: int,
    action: cabc.Callable[[], ActionResult],
    quarantine: cabc.Callable[[str], ActionResult],
) -> None:
    """Run one per-segment action, quarantining whatever a fault names.

    Every operation that can meet a closed fault reports the same way: a
    success appends its action, a closed fault moves the artefact that owns the
    file into quarantine, and any other filesystem failure is recorded as
    ``unreadable``. ``quarantine`` therefore names the artefact rather than the
    reason, because only the caller knows which file a fault implicates.
    """
    try:
        outcome.actions.append(action())
    except SegmentFaultError as fault:
        _quarantine_fault(outcome, sequence, fault.reason, quarantine)
    except OSError:
        outcome.failures.append((sequence, "unreadable"))


def _quarantine_fault(
    outcome: RunOutcome,
    sequence: int,
    reason: str,
    quarantine: cabc.Callable[[str], ActionResult],
) -> None:
    """Move the artefact one closed fault names, recording a failed move.

    A quarantine move can fail for the same filesystem reasons any other
    operation can. The run must record that and carry on with the remaining
    segments rather than end on an unhandled ``OSError``.
    """
    try:
        outcome.actions.append(quarantine(reason))
    except OSError:
        outcome.failures.append((sequence, "unreadable"))
        return
    outcome.failures.append((sequence, reason))


def _archive_one(run: ArchiveRun, entry: SegmentEntry, outcome: RunOutcome) -> None:
    """Archive one sealed source, quarantining it when it is invalid."""
    _quarantining(
        outcome,
        entry.sequence,
        lambda: outcome_archive_files.archive_source(run.context, entry),
        lambda reason: outcome_archive_quarantine.quarantine_source(
            run.context, entry, reason
        ),
    )


def _heal_one(run: ArchiveRun, entry: SegmentEntry, outcome: RunOutcome) -> None:
    """Rebuild one sidecar, quarantining an archive that will not verify."""
    _quarantining(
        outcome,
        entry.sequence,
        lambda: outcome_archive_files.repair_sidecar(run.context, entry),
        lambda reason: outcome_archive_quarantine.quarantine_archive(
            run.context, entry, reason
        ),
    )


def _trim_one(
    run: ArchiveRun,
    entry: SegmentEntry,
    trim: Trim,
    outcome: RunOutcome,
) -> None:
    """Apply one planned trim, quarantining an archive that will not verify."""
    _quarantining(
        outcome,
        entry.sequence,
        lambda: outcome_archive_files.recompress_archive(run.context, entry, trim),
        lambda reason: outcome_archive_quarantine.quarantine_archive(
            run.context, entry, reason
        ),
    )


def _delete_one(run: ArchiveRun, entry: SegmentEntry, outcome: RunOutcome) -> None:
    """Remove one expired archive."""
    try:
        outcome_archive_files.delete_archive(run.context, entry)
    except OSError:
        outcome.failures.append((entry.sequence, "unreadable"))
        return
    outcome.actions.append(ActionResult("deleted", entry.sequence))
