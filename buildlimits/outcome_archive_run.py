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

__all__ = ["ArchiveRun", "RunOutcome", "perform", "quarantined_history"]

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
    quarantine_unsettled : bool
        Whether a quarantine move failed after it may already have renamed the
        file, so the pass cannot tell whether retained history changed.
    """

    actions: list[ActionResult] = dc.field(default_factory=list)
    failures: list[tuple[int, str]] = dc.field(default_factory=list)
    retention_unsettled: bool = False
    quarantine_unsettled: bool = False


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
    the budget asked for is still present. A quarantine during the earlier
    phases also ends the pass before eviction: it moves an archive or sealed
    source out of the retained history, so the totals the plan's budget was
    computed from no longer describe the store, and evicting against them
    could discard history a fresh plan would keep.
    """
    first_action = len(outcome.actions)
    for sealed, archived in run.plan.reconcile:
        _reconcile_one(run, sealed, archived, outcome)
    for entry in run.plan.archive:
        _archive_one(run, entry, outcome)
    for entry in run.plan.heal:
        _heal_one(run, entry, outcome)
    for sequence in run.plan.orphans:
        _quarantine_orphan(run, sequence, outcome)
    if quarantined_history(outcome, first_action):
        return
    _evict(run, outcome, run.plan.trims, run.plan.deletes)


def quarantined_history(outcome: RunOutcome, first_action: int) -> bool:
    """Whether this pass quarantined an archive or a sealed source.

    An orphaned sidecar holds no history, so quarantining it leaves the plan's
    budget totals true and eviction may proceed. Any other quarantine removes
    rows the plan counted, and every such pass returns before its eviction
    steps. A caller that records which eviction it asked for must therefore
    ask this before crediting the plan's trims and deletions: those steps
    never ran, so they stay pending rather than being skipped as done.

    Parameters
    ----------
    outcome : RunOutcome
        The actions and failures accumulated so far.
    first_action : int
        The number of actions recorded before the pass began.

    Returns
    -------
    bool
        True when a quarantine changed, or may have changed, retained history.
    """
    if outcome.quarantine_unsettled:
        # A move that failed after its rename has left the retained history
        # without recording the action, so the plan's totals are just as stale
        # as they are after a recorded quarantine.
        return True
    return any(
        action.action == "quarantined" and action.reason != "orphan_sidecar"
        for action in outcome.actions[first_action:]
    )


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
        # The move renames the file before it syncs the directories, so a
        # failure here can leave the file quarantined with no action recorded.
        # The pass cannot know whether the counted history is still there, so
        # it records the uncertainty and the caller skips eviction.
        outcome.quarantine_unsettled = True
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

    The recorded action names the bound that caused the rewrite, so a report
    reader can tell age eviction from a byte allowance without re-planning.

    Returns
    -------
    bool
        Whether the rewrite succeeded.
    """
    return _quarantining(
        outcome,
        entry.sequence,
        lambda: dc.replace(
            outcome_archive_files.recompress_archive(run.context, entry, trim),
            reason=_trim_reason(trim),
        ),
        lambda reason: outcome_archive_quarantine.quarantine_archive(
            run.context, entry, reason
        ),
    )


def _delete_one(run: ArchiveRun, entry: SegmentEntry, outcome: RunOutcome) -> bool:
    """Remove one evicted archive or sealed source, naming its bound.

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
    outcome.actions.append(
        ActionResult("deleted", entry.sequence, reason=_delete_reason(run, entry))
    )
    return True


def _delete_reason(run: ArchiveRun, entry: SegmentEntry) -> str:
    """Return the bound that made one entry eligible for deletion.

    Age is checked first because the age plan runs before byte planning: an
    entry whose newest row is older than the cutoff would have been deleted
    even if the byte budget had room for it.

    Returns
    -------
    str
        ``age`` when the entry's newest row is older than the cutoff,
        otherwise ``bytes``.
    """
    cutoff = run.context.policy.cutoff(run.context.now)
    return "age" if _is_expired(entry, cutoff) else "bytes"


def _is_expired(entry: SegmentEntry, cutoff: dt.datetime | None) -> bool:
    """Report whether an entry's newest row is older than the cutoff.

    Returns
    -------
    bool
        Whether every row in the entry is past the age bound.
    """
    if cutoff is None or entry.last_observed_at is None:
        return False
    return entry.last_observed_at < cutoff


def _trim_reason(trim: Trim) -> str:
    """Return the bounds one planned trim applies.

    Returns
    -------
    str
        ``age``, ``bytes`` or ``age_and_bytes`` for a composed rewrite.
    """
    by_age = trim.before is not None or trim.records > 0
    by_bytes = trim.allowed_bytes is not None
    if by_age and by_bytes:
        return "age_and_bytes"
    return "age" if by_age else "bytes"
