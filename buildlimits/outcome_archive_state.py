"""Write and recover the state one archive run leaves beside its archives.

The retention floor is the only record that a sequence was deleted rather
than lost, so it is installed before the run's report and does not depend on
that report being writable. A floor that cannot replace ``retention.json`` is
written to ``retention-pending.json`` instead; every reader merges the two
records, so a fault local to one file does not turn completed deletions into
reported gaps. The derivation of the floor lives beside the document it is
written into, leaving the orchestrator to plan and execute only.
"""

import contextlib
import typing as typ

from buildlimits import outcome_archive_codec, outcome_archive_fs, outcome_segment_state
from buildlimits.utc_text import optional_utc_text, utc_now, utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_archive_report import RunReport
    from buildlimits.outcome_archive_run import RunOutcome
    from buildlimits.outcome_segment_inventory import Inventory
    from buildlimits.outcome_segments import SegmentLayout

_STATE_SCHEMA = 1

__all__ = [
    "contiguous_floor",
    "retention_floor",
    "write_deleted_floor",
    "write_floor",
    "write_floor_after_failure",
    "write_state",
]


def retention_floor(
    inventory: Inventory,
    previous: outcome_segment_state.RetentionFloor,
    outcome: RunOutcome,
) -> int:
    """Return the lowest sequence this history intentionally starts at.

    The floor records what retention removed, not what happens to be absent.
    Deriving it from the surviving sequences would raise it past a sequence
    that went missing for another reason, and the next scan would then stop
    reporting that sequence as a gap. It therefore advances only past a
    deleted prefix, and never falls below the first sequence this store ever
    had, so an empty prefix is not reported as missing history.

    Parameters
    ----------
    inventory : Inventory
        The store as it stands after the run.
    previous : RetentionFloor
        The floor the last successful run recorded, or the state that kept it
        from being read. A recorded sequence is reused, a damaged record keeps
        the lowest sequence a store can start at, and only a store that never
        recorded one derives its baseline from the history it has.
    outcome : RunOutcome
        The actions this run performed.

    Returns
    -------
    int
        The retention floor to persist.
    """
    present = [entry.sequence for entry in inventory.effective]
    deleted = {
        action.sequence for action in outcome.actions if action.action == "deleted"
    }
    floor = _floor_baseline(present, deleted, previous)
    # Only a contiguous deleted prefix advances the floor. A sequence that is
    # missing without a deletion may be a gap retention did not create, and
    # stepping past it would stop the next scan reporting it.
    while floor in deleted:
        floor += 1
    return floor


def _floor_baseline(
    present: list[int],
    deleted: set[int],
    previous: outcome_segment_state.RetentionFloor,
) -> int:
    """Return the lowest sequence this history is known to have started at.

    A recorded floor is that sequence. A store that never recorded one is
    fresh, so the lowest sequence it survives with or this run deleted is what
    it is known to have had. A recorded floor that cannot be read is neither:
    the store can no longer prove which absent sequences retention removed, so
    the lowest sequence any store can start at is kept. That reports every
    absent sequence below the first survivor as a gap, which an operator can
    read and dismiss; a baseline derived from the survivors would raise the
    floor over a gap and the next scan would trust the raised value.

    Parameters
    ----------
    present : list[int]
        Sequences the store currently holds.
    deleted : set[int]
        Sequences this run removed.
    previous : RetentionFloor
        The recorded floor, or the state that kept it from being read.

    Returns
    -------
    int
        The sequence the floor starts from.
    """
    if previous.sequence is not None:
        return previous.sequence
    if previous.state == "missing":
        return min([*present, *deleted], default=0)
    return 1


def write_state(layout: SegmentLayout, floor: int, report: RunReport) -> None:
    """Persist the retention floor and the run report durably.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    floor : int
        The retention floor to record.
    report : RunReport
        The completed run's report. A filesystem failure propagates as
        ``OSError`` so the caller can report that no state was installed.
    """
    write_floor(layout, floor, report)
    outcome_archive_fs.atomic_write_json(
        layout.last_run_file, outcome_archive_codec.report_dict(report)
    )


def _floor_document(floor: int, report: RunReport) -> dict[str, object]:
    """Build the floor document a measured run installs.

    Returns
    -------
    dict[str, object]
        The JSON-compatible document ``retention.json`` carries, including
        the limits and retained history the report measured.
    """
    return {
        "schema": _STATE_SCHEMA,
        "floor_sequence": floor,
        "retained_logical_bytes": report.retained_bytes,
        "budget_bytes": report.budget_bytes,
        "cutoff": optional_utc_text(report.cutoff),
        "limits": None if report.limits is None else report.limits.as_dict(),
        "retained": None if report.retained is None else report.retained.as_dict(),
        "updated_at": utc_text(report.finished_at),
    }


def write_floor(layout: SegmentLayout, floor: int, report: RunReport) -> None:
    """Persist the retention floor and the history measured beside it.

    The floor is the only record that a sequence was deleted rather than
    lost, so it is written on its own. A caller whose full state write fails
    can then give this document a second attempt without pretending the run
    report was installed.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    floor : int
        The retention floor to record.
    report : RunReport
        The measurements the floor document describes. A filesystem failure
        propagates as ``OSError``.
    """
    outcome_archive_fs.atomic_write_json(
        layout.retention_file, _floor_document(floor, report)
    )
    _clear_pending(layout)


def write_floor_after_failure(
    layout: SegmentLayout, floor: int, report: RunReport
) -> None:
    """Give the retention floor a second attempt after the state write failed.

    The retention pass has already deleted every sequence the floor advances
    past, and a later run that performs no deletion derives its floor from
    this document alone. A failure of the full state write therefore gets one
    more attempt at the floor, so a transient fault does not turn completed
    deletions into reported gaps.

    That attempt can itself fail for a reason local to ``retention.json``,
    such as a directory squatting on the name, so the floor then falls back
    to ``retention-pending.json``, which readers merge with the primary
    record. Only when both records refuse the write are the deletions left
    reported as gaps, and even then the failure is not raised: the caller
    already returns a failed report, and there is nothing else to record.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    floor : int
        The retention floor to record.
    report : RunReport
        The measurements the floor document describes.
    """
    _keep_floor(layout, _floor_document(floor, report))


def contiguous_floor(start: int, deleted: cabc.Iterable[int]) -> int:
    """Advance a floor across a contiguous run of intentional deletions.

    A single floor number can only say that every sequence below it was
    removed on purpose. Advancing past a deletion that lies beyond a hole
    would excuse the hole as well, so the walk stops at the first sequence
    the run did not remove. A deletion beyond that stop is reported as a gap
    rather than allowed to hide a sequence that may have been lost.

    Parameters
    ----------
    start : int
        The lowest sequence the floor may report as missing.
    deleted : Iterable[int]
        The sequences the run removed from the archive directory.

    Returns
    -------
    int
        The advanced floor.

    Examples
    --------
    >>> contiguous_floor(1, (1, 2, 3))
    4
    >>> contiguous_floor(1, (1, 3))
    2
    >>> contiguous_floor(2, (3,))
    2
    """
    removed = set(deleted)
    floor = start
    while floor in removed:
        floor += 1
    return floor


def write_deleted_floor(
    layout: SegmentLayout, previous: int | None, deleted: tuple[int, ...]
) -> None:
    """Persist the floor implied by deletions from an unmeasurable run.

    The post-run read can fail after the retention pass removed archives. A
    later run that deletes nothing derives its floor from the retention
    records alone, so the floor is written from the completed deletions and
    the floor the last successful run recorded; no further inventory read is
    needed. The floor advances only across a contiguous run of deletions from
    the recorded floor, so a deletion beyond a hole cannot excuse that hole,
    and nothing is written when the run did not advance it. Only the floor and
    its time are recorded. The failed run has no measurement to describe, and
    the previous document's diagnostics describe a state it never observed. A
    write failure falls back to the pending record, and a failure of both is
    suppressed because the caller already reports the run as failed.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    previous : int | None
        The floor the last successful run recorded, if any.
    deleted : tuple[int, ...]
        The sequences this run removed.
    """
    if not deleted:
        return
    start = 1 if previous is None else max(previous, 1)
    floor = contiguous_floor(start, deleted)
    if floor <= start:
        return
    _keep_floor(
        layout,
        {
            "schema": _STATE_SCHEMA,
            "floor_sequence": floor,
            "updated_at": utc_text(utc_now()),
        },
    )


def _keep_floor(layout: SegmentLayout, document: dict[str, object]) -> None:
    """Record a floor without raising when no record can be installed.

    The primary record is tried first because that is the document a reader
    treats as authoritative. A failure local to that path is answered by the
    pending record instead, so the floor survives a fault that does not
    affect the archive directory as a whole. A failure of the fallback as
    well is left unraised: the caller already returns a failed report, and
    the deletions will then be reported as missing history rather than
    hidden.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    document : dict[str, object]
        The floor document to install.
    """
    try:
        outcome_archive_fs.atomic_write_json(layout.retention_file, document)
    except OSError:
        with contextlib.suppress(OSError):
            outcome_archive_fs.atomic_write_json(
                layout.pending_retention_file, document
            )
        return
    _clear_pending(layout)


def _clear_pending(layout: SegmentLayout) -> None:
    """Remove the fallback record once the primary record holds the floor.

    A failure to remove it is harmless: both records are read together and
    the higher floor wins, so a stale fallback can never lower the floor the
    store reports. It is suppressed so a tidy-up fault does not fail a run
    whose state was installed.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    """
    with contextlib.suppress(OSError):
        layout.pending_retention_file.unlink()
