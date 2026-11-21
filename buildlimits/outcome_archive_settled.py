"""Build one settled run's report from the values its caller measured.

The archive command owns the store read and the retention decision, because a
report must describe one version of the store. This module turns those values
into the bounded report and classifies the run, so the report vocabulary
itself stays a plain value with no filesystem read in it.
"""

import typing as typ

from buildlimits.outcome_archive_plan import plan_run
from buildlimits.outcome_archive_report import RunReport, RunStatus
from buildlimits.outcome_archive_report_fields import settled_fields

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_history import ActiveHistory, SettledStore
    from buildlimits.outcome_archive_run import ArchiveRun, RunOutcome
    from buildlimits.outcome_segment_inventory import Inventory

__all__ = ["retention_owes_work", "settled_report"]


def settled_report(
    run: ArchiveRun, outcome: RunOutcome, settled: SettledStore
) -> RunReport:
    """Build the report one settled run's measurements describe.

    The caller owns the store read and the retention decision; this builder
    only maps the settled values and the run's own record into the report.

    Parameters
    ----------
    run : ArchiveRun
        The executed plan and the context its report describes.
    outcome : RunOutcome
        The actions and failures the run recorded.
    settled : SettledStore
        The final inventory and active reading, and whether retention still
        owes work after it.

    Returns
    -------
    RunReport
        The completed run's report, before it is persisted.
    """
    inventory = settled.inventory
    active = settled.active
    return RunReport(
        status=_run_status(
            outcome,
            active,
            retention_unsettled=settled.retention_unsettled,
        ),
        started_at=run.started_at,
        finished_at=run.context.clock(),
        **settled_fields(run, inventory, active),
        actions=tuple(outcome.actions),
        failures=tuple(outcome.failures),
        reason=None if active.has_metadata else active.reason,
    )


def _run_status(
    outcome: RunOutcome, active: ActiveHistory, *, retention_unsettled: bool
) -> RunStatus:
    """Classify a completed pass by what it recorded and could not measure.

    A run that met only a full filesystem is retryable: nothing is corrupt and
    the next timer firing picks the work up once space returns. Any other
    failure needs an operator, so it outranks a space failure in the same run.
    An active file that could not be counted also makes the totals a lower
    bound, and a timer that recorded ``ok`` would hide history a later run can
    see. A scan that could not finish is retryable; content that cannot be
    decoded is corrupt, and that corruption outranks a space failure in the
    same run because it needs an operator rather than another attempt.

    Returns
    -------
    RunStatus
        ``ok``, ``unsettled``, ``no_headroom`` or ``corrupt``.
    """
    if not active.has_metadata and active.reason not in {"unreadable", "unsettled"}:
        return "corrupt"
    if not outcome.failures:
        return "unsettled" if not active.has_metadata or retention_unsettled else "ok"
    if all(reason == "no_headroom" for _sequence, reason in outcome.failures):
        return "no_headroom"
    return "corrupt"


def retention_owes_work(
    inventory: Inventory, active: ActiveHistory, run: ArchiveRun
) -> bool:
    """Whether the final snapshot still calls for retention work.

    The writer never takes the archive lock, so it can append or seal between
    the last retention pass and this read. A report that claimed success over
    that store would tell the timer the budget is met while the next firing
    still has to trim or delete. Only retention decides this: a source sealed
    just before the read is archival work the next firing picks up, and the
    report already counts its bytes.

    Returns
    -------
    bool
        True when the plan still evicts history or defers a budget.
    """
    pending = plan_run(inventory, run.context.policy, now=run.started_at, active=active)
    return bool(pending.trims or pending.deletes or pending.retention_deferred)
