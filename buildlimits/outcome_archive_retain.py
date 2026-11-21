"""Settling the history an archive run leaves behind.

The writer never takes the archive lock, so it can seal a source after the run
has planned and before the follow-up passes look again. Each pass re-reads the
store, performs the archive, reconcile, heal and orphan work it discovers, then
applies the retention the settled inventory plans. The passes are bounded; a
run that still finds work after them reports an unsettled result so the next
timer firing retries instead of claiming success.
"""

import dataclasses as dc
import typing as typ

from buildlimits import outcome_segment_inventory
from buildlimits.outcome_archive_active import active_history
from buildlimits.outcome_archive_history import ActiveHistory
from buildlimits.outcome_archive_plan import PlanSummary, plan_run
from buildlimits.outcome_archive_run import (
    ArchiveRun,
    RunOutcome,
    perform,
    quarantined_history,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_archive_plan import Plan
    from buildlimits.outcome_archive_trim import Trim
    from buildlimits.outcome_segment_inventory import Inventory, SegmentEntry
    from buildlimits.outcome_segments import SegmentLayout

    # A trim is only meaningful against the archive version it was planned
    # for, so each applied trim carries that version's measurement.
    type AppliedTrims = dict[int, tuple[Trim, int, int | None]]

__all__ = ["retain", "settled_snapshot"]

# Follow-up planning passes one run may make after its plan is performed. The
# first settles the sources this run archived, including a source the writer
# sealed mid-run, the second decides a count trim that had to wait for a time
# or byte trim to be applied, and the third applies a count trim the second
# pass discovered. Work a pass cannot finish is left for the next timer firing
# rather than retried without bound, and a check made after the last pass
# distinguishes "settled" from "ran out of passes".
#
# A pass also spends one iteration when the writer seals between the inventory
# scan and the active-file read: the plan would count neither copy, so it is
# abandoned and rebuilt from a fresh pair of reads.
_MAX_RETAIN_PASSES = 3

# Final-report readings retried when the writer moves a source out of the
# active file between the inventory and active scans. Three readings cover a
# seal on each attempt without making the report wait indefinitely.
_SNAPSHOT_ATTEMPTS = 3


class _InventoryChangedError(Exception):
    """The writer sealed a source while a follow-up plan read the store.

    A seal moves rows out of the active file and into a source that a stale
    inventory does not list. Planning from that pair would count neither copy
    and could leave the store over its bound while reporting success, so the
    pass is abandoned and retried from a fresh pair of reads.
    """


def retain(run: ArchiveRun, outcome: RunOutcome) -> bool:
    """Apply retention to the store as it stands after the performed plan.

    The first plan saw the newly published archives as sealed sources, so it
    could not evict them. Re-planning over the settled store keeps the budgets
    true after one successful run instead of leaving the excess for the next
    timer firing. A run that only healed sidecars re-plans too, because the
    first plan had to fall back to each archive's physical size where a
    sidecar was missing or corrupt, which does not describe the logical bytes
    the archive holds.

    A source the writer seals during the run is discovered by the same pass
    and archived before the budget is planned over it, so a record cap can
    still be met in one firing. A trim is not repeated against the archive
    version it already rewrote, but the same trim against an archive the
    active file has since outgrown is a new decision and is applied. An
    archive, reconcile, heal or orphan step that failed is left for the next
    firing, and an eviction failure leaves that entry and every newer eviction
    to the next firing too, so the oldest-first order the budget planned is
    never inverted.

    Parameters
    ----------
    run : ArchiveRun
        The run whose performed plan and context the follow-up uses.
    outcome : RunOutcome
        The actions and failures accumulated so far.

    Returns
    -------
    bool
        Whether the bounded passes settled the store. False means work the run
        discovered is still pending, or a pass could not confirm what it saw,
        so the caller reports a retryable result rather than success.
    """
    applied = _applied_trims(run, outcome)
    removed = _removed(outcome)
    for _pass in range(_MAX_RETAIN_PASSES):
        try:
            pending = _pending_work(run, outcome, applied, removed)
        except _InventoryChangedError:
            # The store moved while this pass read it. Spend another bounded
            # pass on a fresh pair of reads rather than acting on a plan that
            # counted neither the sealed source nor its active rows.
            continue
        if pending is None:
            return True
        failures = len(outcome.failures)
        first_action = len(outcome.actions)
        perform(_follow_up_run(run, pending), outcome)
        if len(outcome.failures) > failures:
            # The failure is already reported, and it outranks a deferred
            # status: the next firing retries what this one could not change.
            return True
        if quarantined_history(outcome, first_action):
            # The pass returned before its eviction steps, so none of this
            # plan's trims or deletions ran. Crediting them would make the
            # next pass filter the still-present entries as already handled
            # and report the store settled with its budget unmet. The plan is
            # rebuilt from the store as the quarantine left it instead.
            continue
        applied.update(_measured(pending.trims))
        removed.update(entry.sequence for entry in pending.deletes)
    try:
        # The last pass may have finished the last operation, so a check made
        # without mutating anything distinguishes a settled store from one
        # that merely ran out of passes.
        return _pending_work(run, outcome, applied, removed) is None
    except _InventoryChangedError:
        return False


def _pending_work(
    run: ArchiveRun,
    outcome: RunOutcome,
    applied: AppliedTrims,
    removed: set[int],
) -> Plan | None:
    """Plan the current store and narrow it to work this run has not done.

    Returns
    -------
    Plan or None
        None when the store plans no further work.
    """
    follow_up = _replan(run)
    failed = {sequence for sequence, _reason in outcome.failures}
    blocked = _failed_eviction(run, outcome)
    archive = tuple(
        entry for entry in follow_up.archive if entry.sequence not in failed
    )
    reconcile = tuple(
        pair for pair in follow_up.reconcile if pair[0].sequence not in failed
    )
    heal = tuple(entry for entry in follow_up.heal if entry.sequence not in failed)
    orphans = tuple(
        sequence for sequence in follow_up.orphans if sequence not in failed
    )
    # An eviction step that failed is not retried. The eviction order is
    # oldest-first, so every step at or after that sequence is left for the
    # next firing: trimming a newer archive would discard rows from history
    # that survives while the entry the budget asked for is still present.
    trims = tuple(
        (entry, trim)
        for entry, trim in follow_up.trims
        if _evictable(entry.sequence, removed, blocked)
        and entry.path.exists()
        and not _already_applied(entry, trim, applied)
    )
    deletes = tuple(
        entry
        for entry in follow_up.deletes
        if _evictable(entry.sequence, removed, blocked) and entry.path.exists()
    )
    narrowed = dc.replace(
        follow_up,
        archive=archive,
        reconcile=reconcile,
        heal=heal,
        orphans=orphans,
        trims=trims,
        deletes=deletes,
    )
    if any((archive, reconcile, heal, orphans, trims, deletes)):
        return narrowed
    # A deferred budget with nothing left to perform cannot settle in this
    # run: no action repairs it. Returning the empty plan keeps the run
    # retryable instead of claiming success over a budget it could not
    # measure, while a store with nothing pending reports settled.
    return narrowed if follow_up.retention_deferred else None


def _already_applied(entry: SegmentEntry, trim: Trim, applied: AppliedTrims) -> bool:
    """Whether this trim already rewrote this version of the archive.

    A count trim and a byte allowance are relative to the document they
    rewrite. Once applied, the sidecar describes the smaller archive, so the
    same trim proposed for the same measurement is the rewrite this run has
    already performed and repeating it would discard rows a later pass has not
    accounted for. The same trim proposed for a differently measured archive -
    the active file grew past the cap and the budget asks for the same number
    of rows again - is a new decision; suppressing it would leave the store
    over its bound while the run reported success.

    Returns
    -------
    bool
        True only when the recorded trim and archive measurement both match.
    """
    return applied.get(entry.sequence) == (
        trim,
        entry.logical_bytes,
        entry.record_count,
    )


def _evictable(sequence: int, removed: set[int], blocked: int | None) -> bool:
    """Whether an eviction step for ``sequence`` may run in this pass.

    Returns
    -------
    bool
        False when the entry is already removed, or when an eviction at or
        before this sequence failed and its newer steps must wait.
    """
    return sequence not in removed and (blocked is None or sequence < blocked)


def _failed_eviction(run: ArchiveRun, outcome: RunOutcome) -> int | None:
    """Return the oldest sequence whose planned eviction failed, if any.

    Returns
    -------
    int or None
        The barrier sequence, or None when no eviction step failed.
    """
    failed = {sequence for sequence, _reason in outcome.failures}
    planned = [entry.sequence for entry, _trim in run.plan.trims]
    planned.extend(entry.sequence for entry in run.plan.deletes)
    return min((sequence for sequence in planned if sequence in failed), default=None)


def _replan(run: ArchiveRun) -> Plan:
    """Plan the store as it stands now, including any concurrent seal.

    The writer never takes the archive lock, so it can seal between the
    inventory scan and the active-file read. Such a seal moves rows out of the
    active file and into a source the first scan does not list, so the pair
    would count neither copy. Reading the inventory again after the active
    file detects that move; a seal after the second read is harmless, because
    the plan then describes the store as it stood when the active file was
    read.

    Returns
    -------
    Plan
        The operations the current inventory and active file call for.

    Raises
    ------
    _InventoryChangedError
        If a seal changed the inventory across the active-file read.
    """
    layout = run.context.layout
    inventory = outcome_segment_inventory.read_inventory(layout)
    active = active_history(
        layout.active,
        cutoff=run.context.policy.cutoff(run.context.now),
        on_retry=run.context.on_retry,
    )
    if outcome_segment_inventory.read_inventory(layout) != inventory:
        msg = "a source was sealed while the follow-up plan read the store"
        raise _InventoryChangedError(msg)
    return plan_run(inventory, run.context.policy, now=run.context.now, active=active)


def settled_snapshot(
    layout: SegmentLayout,
    floor: int | None,
    *,
    cutoff: dt.datetime | None = None,
    on_retry: cabc.Callable[[str, int, str], None] | None = None,
) -> tuple[Inventory, ActiveHistory, bool]:
    """Read one inventory and active version for a plan or a report.

    A seal between the inventory scan and the active-file read moves rows out
    of the active file into a source the scan does not list, so the pair would
    describe neither copy. Reading the inventory again after the active file
    detects that move; the snapshot is accepted only when the two scans agree.
    A seal after the second scan is harmless, because the active read and the
    inventory then describe the store as it stood at that read.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    floor : int | None
        The retention floor the inventory scan reports gaps from, or None when
        the store has recorded none.
    cutoff : datetime | None
        The age cutoff to measure the active rows against, or None to count
        the whole file. The returned history carries both figures either way.
    on_retry : Callable[[str, int, str], None] | None
        Receives each retried read's operation, attempt number and category.

    Returns
    -------
    tuple[Inventory, ActiveHistory, bool]
        The last inventory and active readings, and whether they describe one
        version of the store. A writer that keeps sealing across every attempt
        leaves the caller to report an unsettled result so the timer retries.
    """
    inventory = outcome_segment_inventory.read_inventory(layout, retention_floor=floor)
    active = ActiveHistory()
    for attempt in range(_SNAPSHOT_ATTEMPTS):
        active = active_history(layout.active, cutoff=cutoff, on_retry=on_retry)
        after = outcome_segment_inventory.read_inventory(layout, retention_floor=floor)
        if after == inventory:
            return inventory, active, True
        inventory = after
        if on_retry is not None:
            on_retry("snapshot", attempt + 1, "inventory_changed")
    return inventory, active, False


def _follow_up_run(run: ArchiveRun, plan: Plan) -> ArchiveRun:
    """Bind one follow-up plan to the context of the performed run.

    Returns
    -------
    ArchiveRun
        The follow-up plan as an executable run.
    """
    return ArchiveRun(
        context=run.context,
        plan=plan,
        summary=PlanSummary.of(plan),
        started_at=run.started_at,
    )


def _applied_trims(run: ArchiveRun, outcome: RunOutcome) -> AppliedTrims:
    """Return the trims the performed plan applied, with the archive version.

    The measurement is the one the performed plan saw, so a later plan that
    proposes the same trim against a differently measured archive is known to
    be making a new decision.

    Returns
    -------
    AppliedTrims
        The trim and source measurement of every applied trim, by sequence.
    """
    trimmed = {
        action.sequence for action in outcome.actions if action.action == "trimmed"
    }
    return _measured(
        (entry, trim) for entry, trim in run.plan.trims if entry.sequence in trimmed
    )


def _measured(trims: cabc.Iterable[tuple[SegmentEntry, Trim]]) -> AppliedTrims:
    """Pair each trim with the archive version it was decided from.

    A trim is only comparable with a later plan when it carries the
    measurement it was planned against, so the trims a plan applied and the
    trims a later plan proposes are both recorded through this one shape.

    Returns
    -------
    AppliedTrims
        The trim and source measurement of every given trim, by sequence.
    """
    return {
        entry.sequence: (trim, entry.logical_bytes, entry.record_count)
        for entry, trim in trims
    }


def _removed(outcome: RunOutcome) -> set[int]:
    """Return the sequences this run removed, which no later pass can reuse.

    Only a deletion removes an entry. A quarantined *source* leaves its
    archive in place, and that archive is still eligible for the budget a
    follow-up plan recomputes.

    Returns
    -------
    set[int]
        The sequences removed whole.
    """
    return {action.sequence for action in outcome.actions if action.action == "deleted"}
