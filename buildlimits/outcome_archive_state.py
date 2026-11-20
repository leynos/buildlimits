"""Persist one archive run's retention floor and report.

The floor records what retention removed, not what happens to be absent, and
the report is the timer's only view of the run. Both are written after the
final snapshot, so the state an operator reads describes the same version of
the store the report counts. Keeping the derivation beside the document it is
written into leaves the orchestrator to plan and execute only.
"""

import typing as typ

from buildlimits import outcome_archive_fs, outcome_segment_state
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
        from being read; only a recorded sequence is reused as the baseline.
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
    # A fresh store starts at the lowest sequence it is known to have had:
    # either one that survives, or one this run deleted. Later runs keep the
    # floor they recorded.
    baseline = (
        previous.sequence
        if previous.sequence is not None
        else min([*present, *deleted], default=0)
    )
    # Only a contiguous deleted prefix advances the floor. A sequence that is
    # missing without a deletion may be a gap retention did not create, and
    # stepping past it would stop the next scan reporting it.
    floor = baseline
    while floor in deleted:
        floor += 1
    return floor


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
    outcome_archive_fs.atomic_write_json(layout.last_run_file, report.as_dict())
