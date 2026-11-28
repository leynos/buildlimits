"""Executing one maintenance plan, one closed operation at a time.

Every per-segment operation is wrapped here so one unreadable file becomes a
recorded failure instead of ending the run. The order is fixed: reconcile first
so a half-published pair is settled before anything is archived again, then
clear stale rewrite markers and re-derive unmeasurable sealed metadata, archive,
heal, quarantine orphans, trim and finally delete.
"""

import dataclasses as dc
import typing as typ

from buildlimits import (
    outcome_archive_evict,
    outcome_archive_files,
    outcome_archive_quarantine,
    outcome_archive_reconcile,
)
from buildlimits.outcome_archive_context import ActionResult
from buildlimits.outcome_archive_replace import end_sealed_replacement
from buildlimits.outcome_archive_verify import OutputFaultError, SegmentFaultError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_archive_plan import Plan, PlanSummary
    from buildlimits.outcome_segment_inventory import SegmentEntry


class _EntryAction(typ.Protocol):
    """One per-entry operation the shared fault boundary wraps."""

    def __call__(self, context: ArchiveContext, entry: SegmentEntry) -> ActionResult:
        """Perform the operation for one entry."""


class _EntryQuarantine(typ.Protocol):
    """One quarantine move the shared fault boundary may take."""

    def __call__(
        self, context: ArchiveContext, entry: SegmentEntry, reason: str
    ) -> ActionResult:
        """Move the artefact one closed fault names into quarantine."""


@dc.dataclass(frozen=True, slots=True)
class _EntryActor:
    """The operation and quarantine move one per-entry step is made of.

    Attributes
    ----------
    act : _EntryAction
        The operation to perform.
    quarantine : _EntryQuarantine
        Where a closed fault moves the artefact it names.
    """

    act: _EntryAction
    quarantine: _EntryQuarantine


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

    Repairs run before archival and eviction because neither can measure a
    source whose metadata is untrusted. A retention-only plan has no archival
    step at all, so its repairs are what make the follow-up plan able to
    decide the age, byte and record bounds for sealed history.
    """
    first_action = len(outcome.actions)
    # The actors are built here, at call time, so a caller that replaces one of
    # the per-entry operations sees its replacement used.
    repair = _EntryActor(
        outcome_archive_files.repair_sealed_metadata,
        outcome_archive_quarantine.quarantine_source,
    )
    archive = _EntryActor(
        outcome_archive_files.archive_source,
        outcome_archive_quarantine.quarantine_source,
    )
    heal = _EntryActor(
        outcome_archive_files.repair_sidecar,
        outcome_archive_quarantine.quarantine_archive,
    )
    for sealed, archived in run.plan.reconcile:
        _reconcile_one(run, sealed, archived, outcome)
    for sequence in run.plan.stale_sealed_markers:
        _clear_stale_marker(run, sequence, outcome)
    for entry in run.plan.sealed_repairs:
        _per_entry(run, entry, outcome, repair)
    for entry in run.plan.archive:
        _per_entry(run, entry, outcome, archive)
    for entry in run.plan.heal:
        _per_entry(run, entry, outcome, heal)
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
            outcome_archive_reconcile.reconcile_duplicate(run.context, sealed, archived)
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


def _clear_stale_marker(run: ArchiveRun, sequence: int, outcome: RunOutcome) -> None:
    """Remove one rewrite marker whose sealed source is already gone."""
    try:
        end_sealed_replacement(run.context.layout.sealed_dir, sequence)
    except OSError as error:
        outcome.failures.append((sequence, outcome_archive_evict.failure_reason(error)))
        return
    outcome.actions.append(ActionResult("repaired", sequence, reason="stale_marker"))


def _per_entry(
    run: ArchiveRun,
    entry: SegmentEntry,
    outcome: RunOutcome,
    actor: _EntryActor,
) -> None:
    """Run one per-entry operation behind the shared fault boundary.

    Only the caller knows which file a closed fault implicates, so the actor
    names both the operation and the quarantine move it owns.
    """
    outcome_archive_evict.quarantining(
        outcome,
        entry.sequence,
        lambda: actor.act(run.context, entry),
        lambda reason: actor.quarantine(run.context, entry, reason),
    )
