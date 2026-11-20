"""Executing one archive plan, one closed operation at a time.

Every per-segment operation is wrapped here so one unreadable file becomes a
recorded failure instead of ending the run. The order is fixed: reconcile first
so a half-published pair is settled before anything is archived again, then
archive, heal, quarantine orphans, trim and finally delete.
"""

import dataclasses as dc
import errno
import typing as typ

from buildlimits import outcome_archive_files, outcome_archive_quarantine
from buildlimits.outcome_archive_context import ActionResult, ArchiveContext
from buildlimits.outcome_archive_verify import OutputFaultError, SegmentFaultError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_archive_plan import Plan, PlanSummary
    from buildlimits.outcome_archive_trim import Trim
    from buildlimits.outcome_segment_inventory import SegmentEntry

__all__ = ["ArchiveRun", "RunOutcome", "perform"]

# A filesystem that cannot hold this run's output. The headroom check reads
# free space once before the pass, so another writer can still fill it while
# this run works; that is a temporary condition the next firing retries.
_NO_SPACE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})


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
    """The mutations one run accumulates while it performs its plan.

    Attributes
    ----------
    actions : list[ActionResult]
        The operations the run performed.
    failures : list[tuple[int, str]]
        Sequences that could not be processed, with a closed reason.
    retention_unsettled : bool
        Whether the bounded follow-up passes left retention work pending.
    """

    actions: list[ActionResult] = dc.field(default_factory=list)
    failures: list[tuple[int, str]] = dc.field(default_factory=list)
    retention_unsettled: bool = False


def failure_reason(error: OSError) -> str:
    """Name one filesystem failure with the report's closed vocabulary.

    A full filesystem is temporary: the store is intact, and the next timer
    firing retries once space returns. Anything else is reported as
    ``unreadable``, the reason an operator must look at.

    Returns
    -------
    str
        ``no_headroom`` for a full filesystem, else ``unreadable``.
    """
    return "no_headroom" if error.errno in _NO_SPACE_ERRNOS else "unreadable"


def perform(run: ArchiveRun, outcome: RunOutcome) -> None:
    """Execute every planned operation in a fixed order.

    The eviction steps run last, interleaved by sequence, so an older entry is
    settled before any newer one is rewritten or removed. A failure ends that
    phase: the steps after it would act on newer history while the older entry
    the budget asked for is still present.
    """
    for sealed, archived in run.plan.reconcile:
        _reconcile_one(run, sealed, archived, outcome)
    for entry in run.plan.archive:
        _archive_one(run, entry, outcome)
    for entry in run.plan.heal:
        _heal_one(run, entry, outcome)
    for sequence in run.plan.orphans:
        _quarantine_orphan(run, sequence, outcome)
    _evict(run, outcome, run.plan.trims, run.plan.deletes)


def _evict(
    run: ArchiveRun,
    outcome: RunOutcome,
    trims: cabc.Iterable[tuple[SegmentEntry, Trim]],
    deletes: cabc.Iterable[SegmentEntry],
) -> None:
    """Apply one plan's trims and deletions oldest-first, stopping at a failure.

    A trim discards rows from history that survives, and a deletion removes a
    whole entry, so the two operations must follow the same oldest-first walk
    the planner used. Running every trim before every deletion would rewrite a
    newer archive even when an older entry could not be removed, discarding
    rows from history while the entry the budget asked for remained. The first
    failure therefore ends the phase and leaves newer history untouched for the
    next run, which sees it in ``outcome.failures``.

    Parameters
    ----------
    run : ArchiveRun
        The run whose context the operations use.
    outcome : RunOutcome
        The actions and failures accumulated so far.
    trims : Iterable[tuple[SegmentEntry, Trim]]
        Archives to rewrite with a prefix trim.
    deletes : Iterable[SegmentEntry]
        Archives and sealed sources to remove entirely.
    """
    steps: list[tuple[SegmentEntry, Trim | None]] = []
    steps.extend(trims)
    steps.extend((entry, None) for entry in deletes)
    for entry, trim in sorted(steps, key=lambda step: step[0].sequence):
        applied = (
            _delete_one(run, entry, outcome)
            if trim is None
            else _trim_one(run, entry, trim, outcome)
        )
        if not applied:
            return


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
    except (OutputFaultError, SegmentFaultError) as fault:
        # Re-archiving a quarantined duplicate can meet the same closed faults
        # any other archive pass can. The pair is left for the next run rather
        # than ending this one.
        outcome.failures.append((sealed.sequence, fault.reason))
    except OSError as error:
        outcome.failures.append((sealed.sequence, failure_reason(error)))


def _quarantine_orphan(run: ArchiveRun, sequence: int, outcome: RunOutcome) -> None:
    """Quarantine one orphaned sidecar, recording a filesystem failure.

    An orphan says nothing about whether the rest of the plan is safe to run,
    so a failure here is recorded and the run continues to trim and delete.
    """
    try:
        outcome.actions.append(
            outcome_archive_quarantine.remove_orphan_sidecar(run.context, sequence)
        )
    except OSError as error:
        outcome.failures.append((sequence, failure_reason(error)))


def _quarantining(
    outcome: RunOutcome,
    sequence: int,
    action: cabc.Callable[[], ActionResult],
    quarantine: cabc.Callable[[str], ActionResult],
) -> bool:
    """Run one per-segment action, quarantining whatever a fault names.

    Every operation that can meet a closed fault reports the same way: a
    success appends its action, a closed fault moves the artefact that owns the
    file into quarantine, and any other filesystem failure is recorded as
    ``unreadable`` or, when the filesystem is full, ``no_headroom``.
    ``quarantine`` therefore names the artefact rather than the reason, because
    only the caller knows which file a fault implicates.

    Returns
    -------
    bool
        Whether the action succeeded. A caller that performs dependent work
        uses this to stop before acting on newer history.
    """
    try:
        outcome.actions.append(action())
    except OutputFaultError as fault:
        # The source was readable and the fault is in this run's own output,
        # so the artefact must survive for the next attempt.
        outcome.failures.append((sequence, fault.reason))
        return False
    except SegmentFaultError as fault:
        _quarantine_fault(outcome, sequence, fault.reason, quarantine)
        return False
    except OSError as error:
        outcome.failures.append((sequence, failure_reason(error)))
        return False
    return True


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
    except OSError as error:
        outcome.failures.append((sequence, failure_reason(error)))
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
) -> bool:
    """Apply one planned trim, quarantining an archive that will not verify.

    Returns
    -------
    bool
        Whether the rewrite succeeded.
    """
    return _quarantining(
        outcome,
        entry.sequence,
        lambda: outcome_archive_files.recompress_archive(run.context, entry, trim),
        lambda reason: outcome_archive_quarantine.quarantine_archive(
            run.context, entry, reason
        ),
    )


def _delete_one(run: ArchiveRun, entry: SegmentEntry, outcome: RunOutcome) -> bool:
    """Remove one evicted archive or sealed source.

    Returns
    -------
    bool
        Whether the entry was removed.
    """
    try:
        if entry.state == "sealed":
            outcome_archive_files.delete_source(run.context, entry)
        else:
            outcome_archive_files.delete_archive(run.context, entry)
    except OSError as error:
        outcome.failures.append((entry.sequence, failure_reason(error)))
        return False
    outcome.actions.append(ActionResult("deleted", entry.sequence))
    return True
