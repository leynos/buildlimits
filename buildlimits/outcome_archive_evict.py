"""Apply one plan's eviction steps oldest-first.

A trim discards rows from history that survives, and a deletion removes a
whole entry, so both follow the same oldest-first walk the planner used. The
first failure ends the phase and leaves newer history untouched for the next
run, which sees it in the outcome's failures.
"""

import dataclasses as dc
import errno
import typing as typ

from buildlimits import (
    outcome_archive_files,
    outcome_archive_fs,
    outcome_archive_quarantine,
    outcome_sealed_trim,
)
from buildlimits.outcome_archive_context import ActionResult
from buildlimits.outcome_archive_verify import OutputFaultError, SegmentFaultError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_archive_run import ArchiveRun, RunOutcome
    from buildlimits.outcome_archive_trim import Trim
    from buildlimits.outcome_segment_inventory import SegmentEntry

__all__ = ["evict", "failure_reason", "quarantining"]


def _trim_one(
    run: ArchiveRun,
    entry: SegmentEntry,
    trim: Trim,
    outcome: RunOutcome,
) -> bool:
    """Apply one planned trim to whichever form the entry has.

    The recorded action names the bounds that caused the rewrite, so a report
    reader can tell an age eviction, a byte allowance and a record cap apart
    without re-planning. An archive is recompressed; a sealed source is
    rewritten in place as plain JSON Lines, because a retention-only run never
    compresses history.

    Returns
    -------
    bool
        Whether the rewrite succeeded.
    """
    reason = _trim_reason(trim)
    if entry.state == "sealed":
        return _trim_sealed(run, entry, trim, outcome)
    return quarantining(
        outcome,
        entry.sequence,
        lambda: dc.replace(
            outcome_archive_files.recompress_archive(run.context, entry, trim),
            reason=reason,
        ),
        lambda fault: outcome_archive_quarantine.quarantine_archive(
            run.context, entry, fault
        ),
    )


def _trim_sealed(
    run: ArchiveRun,
    entry: SegmentEntry,
    trim: Trim,
    outcome: RunOutcome,
) -> bool:
    """Rewrite one sealed source in place, reporting a failed removal as done.

    A trim that keeps no records removes the source instead. That removal is
    proven the same way a planned deletion is: a fault after the rows are gone
    is still reported, but the completed deletion is recorded too, so the floor
    can advance over the sequence rather than reporting it as lost history for
    ever.

    Returns
    -------
    bool
        Whether the rewrite or removal succeeded.
    """
    reason = _trim_reason(trim)
    try:
        action = outcome_sealed_trim.trim_sealed_source(run.context, entry, trim)
    except OutputFaultError as fault:
        # The source was readable and the fault is in this run's own output,
        # so the source must survive for the next attempt.
        outcome.failures.append((entry.sequence, fault.reason))
        return False
    except SegmentFaultError as fault:
        _quarantine_fault(
            outcome,
            entry.sequence,
            fault.reason,
            lambda fault_reason: outcome_archive_quarantine.quarantine_source(
                run.context, entry, fault_reason
            ),
        )
        return False
    except OSError as error:
        outcome.failures.append((entry.sequence, failure_reason(error)))
        if not outcome_archive_fs.is_proven_absent(entry.path):
            return False
        outcome.actions.append(ActionResult("deleted", entry.sequence, reason=reason))
        return True
    outcome.actions.append(dc.replace(action, reason=reason))
    return True


def _delete_one(
    run: ArchiveRun, entry: SegmentEntry, cause: str, outcome: RunOutcome
) -> bool:
    """Remove one planned entry, naming the bound that made it eligible.

    The removal is only refused when the entry itself is proven to survive. A
    fault that follows its removal, such as a sidecar cleanup or a directory
    sync, leaves the sequence gone from the store, and the floor is the only
    record that it was deleted on purpose. The failure is still reported, but
    the completed deletion is recorded as well, rather than leaving the
    sequence to be reported as lost history for ever. An entry the run cannot
    inspect is not evidence of either outcome, so it keeps the failure without
    advancing the floor over a sequence the store may still hold.

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
        if not outcome_archive_fs.is_proven_absent(entry.path):
            return False
    outcome.actions.append(ActionResult("deleted", entry.sequence, reason=cause))
    return True


def _trim_reason(trim: Trim) -> str:
    """Return the bounds one planned trim applies.

    Each bound a trim serves is visible in the field it acts on, so the cause
    is read from the trim rather than inferred from the policy. A count-driven
    rewrite therefore names ``records`` even when no byte budget is set, and
    an age or byte bound that did not cause the rewrite is never named.

    Returns
    -------
    str
        The closed names of the bounds, joined in a fixed order: ``age``,
        ``bytes``, ``records``, ``age_and_bytes``, ``age_and_records``,
        ``bytes_and_records`` or ``age_and_bytes_and_records``.
    """
    bounds = [
        name
        for name, is_applied in (
            ("age", trim.before is not None),
            ("bytes", trim.allowed_bytes is not None),
            ("records", trim.records > 0),
        )
        if is_applied
    ]
    return "_and_".join(bounds)


def evict(run: ArchiveRun, outcome: RunOutcome) -> None:
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
    """
    causes = dict(run.plan.eviction_causes)
    steps: list[tuple[SegmentEntry, Trim | None, str | None]] = [
        (entry, trim, None) for entry, trim in run.plan.trims
    ]
    steps.extend((entry, None, causes[entry.sequence]) for entry in run.plan.deletes)
    for entry, trim, cause in sorted(steps, key=lambda step: step[0].sequence):
        applied = (
            _delete_one(run, entry, cause or "bytes", outcome)
            if trim is None
            else _trim_one(run, entry, trim, outcome)
        )
        if not applied:
            return


# A filesystem that cannot hold this run's output. The headroom check reads
# free space once before the pass, so another writer can still fill it while
# this run works; that is a temporary condition the next firing retries.
_NO_SPACE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})


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


def quarantining(
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
    operation can. The move renames the file before it syncs the directories,
    so a failure here can leave the file quarantined with no action recorded.
    The pass cannot know whether the counted history is still there, so it
    records the uncertainty and the caller stops eviction.
    """
    try:
        outcome.actions.append(quarantine(reason))
    except OSError as error:
        outcome.quarantine_unsettled = True
        outcome.failures.append((sequence, failure_reason(error)))
        return
    outcome.failures.append((sequence, reason))
