"""The report path for a run whose work succeeded but was not measurable.

An archive run can publish its archives and evictions and then fail while
measuring or persisting the result. The work is real, so the deletions it
completed are recorded before the report is built, and a full filesystem is
reported as retryable rather than as a failure.
"""

import typing as typ

from buildlimits import outcome_archive_state, outcome_segment_state
from buildlimits.outcome_archive_report_builders import after_work_failure
from buildlimits.outcome_archive_run import deleted_sequences

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_report import RunReport
    from buildlimits.outcome_archive_run import ArchiveRun, RunOutcome

__all__ = ["unmeasured_after_work"]


def unmeasured_after_work(
    run: ArchiveRun,
    outcome: RunOutcome,
    reason: str,
) -> tuple[int | None, RunReport]:
    """Record the deletions a run completed, then report its unmeasured result.

    The archives and evictions are already published. The deletions are the
    only record the floor has, so they are written before the unmeasurable
    result is reported. The retention passes and the final plan both re-read
    the active file, so a cap either cannot settle takes this path.

    Parameters
    ----------
    run : ArchiveRun
        The plan and context this run executed.
    outcome : RunOutcome
        The actions and failures this run performed.
    reason : str
        The closed reason the result could not be measured.

    Returns
    -------
    tuple[int | None, RunReport]
        No recorded floor, and the report carrying the completed work.
    """
    outcome_archive_state.write_deleted_floor(
        run.context.layout,
        outcome_segment_state.read_retention_floor(run.context.layout).sequence,
        deleted_sequences(outcome.actions),
        now=run.context.clock(),
    )
    return None, after_work_failure(run, outcome, reason)
