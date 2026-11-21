"""Persist one archive run's retention floor and report.

The floor records what retention removed, not what happens to be absent, and
the report is the timer's only view of the run. Both are written after the
final snapshot, so the state an operator reads describes the same version of
the store the report counts. Keeping the derivation beside the document it is
written into leaves the orchestrator to plan and execute only.
"""

import typing as typ

from buildlimits import outcome_archive_codec, outcome_archive_fs, outcome_segment_state
from buildlimits.utc_text import optional_utc_text, utc_text

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_report import RunReport
    from buildlimits.outcome_archive_run import RunOutcome
    from buildlimits.outcome_segment_inventory import Inventory
    from buildlimits.outcome_segments import SegmentLayout

_STATE_SCHEMA = 1

__all__ = ["retention_floor", "write_state"]


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
        The layout whose state files are replaced.
    floor : int
        The retention floor this run recorded.
    report : RunReport
        The completed run's report.
    """
    outcome_archive_fs.atomic_write_json(
        layout.retention_file,
        {
            "schema": _STATE_SCHEMA,
            "floor_sequence": floor,
            "retained_logical_bytes": report.retained_bytes,
            "budget_bytes": report.budget_bytes,
            "cutoff": optional_utc_text(report.cutoff),
            "updated_at": utc_text(report.finished_at),
        },
    )
    outcome_archive_fs.atomic_write_json(
        layout.last_run_file, outcome_archive_codec.report_dict(report)
    )
