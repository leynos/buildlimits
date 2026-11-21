"""Executing one archive plan, one closed operation at a time.

Every per-segment operation is wrapped here so one unreadable file becomes a
recorded failure instead of ending the run. The order is fixed: reconcile first
so a half-published pair is settled before anything is archived again, then
archive, heal, quarantine orphans, trim and finally delete.
"""

import dataclasses as dc
import typing as typ

from buildlimits import (
    outcome_archive_evict,
    outcome_archive_files,
    outcome_archive_quarantine,
)
from buildlimits.outcome_archive_verify import OutputFaultError, SegmentFaultError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_archive_context import ActionResult, ArchiveContext
    from buildlimits.outcome_archive_plan import Plan, PlanSummary
    from buildlimits.outcome_segment_inventory import SegmentEntry

__all__ = [
    "ArchiveRun",
    "RunOutcome",
    "deleted_sequences",
    "perform",
    "quarantined_history",
]


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


def deleted_sequences(actions: cabc.Iterable[ActionResult]) -> tuple[int, ...]:
    """Return the sequences a run's recorded actions removed from the archive.

    The actions are passed rather than a whole outcome or report, because both
    the outcomes a run accumulates and the report it publishes list the same
    ``ActionResult`` values.

    Returns
    -------
    tuple[int, ...]
        Each deleted sequence, in the order the run removed it.
    """
    return tuple(action.sequence for action in actions if action.action == "deleted")


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
    outcome_archive_evict.evict(run, outcome)


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
        outcome.failures.append((
            sealed.sequence,
            outcome_archive_evict.failure_reason(error),
        ))


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
        outcome.failures.append((sequence, outcome_archive_evict.failure_reason(error)))


def _archive_one(run: ArchiveRun, entry: SegmentEntry, outcome: RunOutcome) -> None:
    """Archive one sealed source, quarantining it when it is invalid."""
    outcome_archive_evict.quarantining(
        outcome,
        entry.sequence,
        lambda: outcome_archive_files.archive_source(run.context, entry),
        lambda reason: outcome_archive_quarantine.quarantine_source(
            run.context, entry, reason
        ),
    )


def _heal_one(run: ArchiveRun, entry: SegmentEntry, outcome: RunOutcome) -> None:
    """Rebuild one sidecar, quarantining an archive that will not verify."""
    outcome_archive_evict.quarantining(
        outcome,
        entry.sequence,
        lambda: outcome_archive_files.repair_sidecar(run.context, entry),
        lambda reason: outcome_archive_quarantine.quarantine_archive(
            run.context, entry, reason
        ),
    )
