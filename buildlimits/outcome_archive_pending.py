"""The work one retention pass has not performed yet.

A run performs several bounded passes, and each pass must not repeat what the
previous one did. This module narrows a fresh plan to the steps that remain:
the sequences a step already failed on, the trims already applied against the
version of the archive they were measured from, and the evictions an earlier
failure must not overtake. Keeping the bookkeeping here lets the pass loop read
as the order it enforces.
"""

import dataclasses as dc
import operator
import typing as typ

from buildlimits.outcome_archive_fs import is_proven_absent

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_archive_plan import Plan
    from buildlimits.outcome_archive_run import ArchiveRun, RunOutcome
    from buildlimits.outcome_archive_trim import Trim
    from buildlimits.outcome_segment_inventory import SegmentEntry

    # A trim is only meaningful against the archive version it was planned
    # for, so each applied trim carries that version's measurement.
    type AppliedTrims = dict[int, tuple[Trim, int, int | None]]

__all__ = [
    "Done",
    "applied_trims",
    "failed_eviction",
    "has_work",
    "measured",
    "narrow",
    "removed",
]


@dc.dataclass(frozen=True, slots=True)
class Done:
    """What one run has already performed, so a pass does not repeat it.

    Attributes
    ----------
    failed : set[int]
        Sequences whose step failed and must wait for the next firing.
    blocked : int | None
        The oldest sequence whose eviction failed, or None.
    applied : AppliedTrims
        The trims this run applied, with the archive version each was
        decided from.
    removed : set[int]
        Sequences this run removed whole.
    """

    failed: set[int]
    blocked: int | None
    applied: AppliedTrims
    removed: set[int]


def narrow(follow_up: Plan, done: Done) -> Plan:
    """Drop the steps this run has already performed or must not retry.

    Returns
    -------
    Plan
        The plan narrowed to the work one more pass may perform.
    """
    trims, deletes = _eviction_steps(follow_up, done)
    return dc.replace(
        follow_up,
        archive=_unfailed(
            follow_up.archive, done.failed, operator.attrgetter("sequence")
        ),
        reconcile=_unfailed_pairs(follow_up.reconcile, done.failed),
        heal=_unfailed(follow_up.heal, done.failed, operator.attrgetter("sequence")),
        sealed_repairs=_repairable(follow_up.sealed_repairs, done.failed),
        stale_sealed_markers=_unfailed(
            follow_up.stale_sealed_markers, done.failed, _itself
        ),
        orphans=_unfailed(follow_up.orphans, done.failed, _itself),
        trims=trims,
        deletes=deletes,
    )


def _itself(sequence: int) -> int:
    """Return a sequence unchanged, for callers that already hold one.

    Returns
    -------
    int
        The same sequence.
    """
    return sequence


def _unfailed[Item](
    items: cabc.Iterable[Item],
    failed: set[int],
    sequence_of: cabc.Callable[[Item], int],
) -> tuple[Item, ...]:
    """Return the items whose step has not failed.

    Returns
    -------
    tuple[Item, ...]
        The items a later pass may act on.
    """
    return tuple(item for item in items if sequence_of(item) not in failed)


def _unfailed_pairs(
    pairs: cabc.Iterable[tuple[SegmentEntry, SegmentEntry]], failed: set[int]
) -> tuple[tuple[SegmentEntry, SegmentEntry], ...]:
    """Return the duplicate pairs whose reconciliation has not failed.

    Returns
    -------
    tuple[tuple[SegmentEntry, SegmentEntry], ...]
        The pairs a later pass may settle.
    """
    return tuple(pair for pair in pairs if pair[0].sequence not in failed)


def _repairable(
    entries: cabc.Iterable[SegmentEntry], failed: set[int]
) -> tuple[SegmentEntry, ...]:
    """Return the sealed sources a later pass may still repair.

    An entry that is proven absent has nothing to repair, while one that cannot
    be inspected must stay so its failure is recorded against it.

    Returns
    -------
    tuple[SegmentEntry, ...]
        The sources a later pass may describe.
    """
    return tuple(
        entry
        for entry in entries
        if entry.sequence not in failed and not is_proven_absent(entry.path)
    )


def _eviction_steps(
    follow_up: Plan, done: Done
) -> tuple[
    tuple[tuple[SegmentEntry, Trim], ...],
    tuple[SegmentEntry, ...],
]:
    """Return the trims and deletions a later pass may still perform.

    An eviction step that failed is not retried, and every step at or after it
    waits for the next firing: the order is oldest-first, so trimming newer
    history while the older entry remains would discard rows the budget asked
    to keep. A trim already applied against the same measured archive is not
    repeated.

    Returns
    -------
    tuple
        The trims and deletions the pass may perform.
    """
    trims = tuple(
        (entry, trim)
        for entry, trim in follow_up.trims
        if evictable(entry.sequence, done.removed, done.blocked)
        and not is_proven_absent(entry.path)
        and not already_applied(entry, trim, done.applied)
    )
    deletes = tuple(
        entry
        for entry in follow_up.deletes
        if evictable(entry.sequence, done.removed, done.blocked)
        and not is_proven_absent(entry.path)
    )
    return trims, deletes


def has_work(plan: Plan) -> bool:
    """Whether a narrowed plan still holds an operation to perform.

    Returns
    -------
    bool
        True when any step remains.
    """
    return bool(
        plan.archive
        or plan.reconcile
        or plan.heal
        or plan.sealed_repairs
        or plan.stale_sealed_markers
        or plan.orphans
        or plan.trims
        or plan.deletes
    )


def already_applied(entry: SegmentEntry, trim: Trim, applied: AppliedTrims) -> bool:
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


def evictable(sequence: int, removed: set[int], blocked: int | None) -> bool:
    """Whether an eviction step for ``sequence`` may run in this pass.

    Returns
    -------
    bool
        False when the entry is already removed, or when an eviction at or
        before this sequence failed and its newer steps must wait.
    """
    return sequence not in removed and (blocked is None or sequence < blocked)


def failed_eviction(run: ArchiveRun, outcome: RunOutcome) -> int | None:
    """Return the oldest sequence whose planned eviction failed, if any.

    Returns
    -------
    int or None
        The barrier sequence, or None when no eviction step failed.
    """
    failed = {sequence for sequence, _reason in outcome.failures}
    removed = {
        action.sequence for action in outcome.actions if action.action == "deleted"
    }
    planned = [entry.sequence for entry, _trim in run.plan.trims]
    planned.extend(entry.sequence for entry in run.plan.deletes)
    # A removal that completed before a later step failed is recorded as both a
    # failure and a deletion. The older entry is gone, so it is no barrier to
    # the newer steps the same plan ordered after it.
    return min(
        (
            sequence
            for sequence in planned
            if sequence in failed and sequence not in removed
        ),
        default=None,
    )


def applied_trims(run: ArchiveRun, outcome: RunOutcome) -> AppliedTrims:
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
    return measured(
        (entry, trim) for entry, trim in run.plan.trims if entry.sequence in trimmed
    )


def measured(trims: cabc.Iterable[tuple[SegmentEntry, Trim]]) -> AppliedTrims:
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


def removed(outcome: RunOutcome) -> set[int]:
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
