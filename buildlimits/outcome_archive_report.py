"""The bounded report and status vocabulary one archive run produces.

The report is the command's only user-facing surface: it names no filesystem
path and carries closed action and reason vocabularies. Rendering it and
mapping its status to a process exit code are adapters' work, so the model
stays a plain value that a caller can compare and pass on.
"""

import dataclasses as dc
import typing as typ

from buildlimits import outcome_archive_retain
from buildlimits.outcome_archive_context import ignore_retry
from buildlimits.outcome_archive_history import ActiveHistory, observed_history
from buildlimits.outcome_archive_plan import plan_run
from buildlimits.utc_text import utc_now

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.metrics import MetricsPort
    from buildlimits.outcome_archive_context import ActionResult
    from buildlimits.outcome_archive_plan import PlanSummary
    from buildlimits.outcome_archive_run import ArchiveRun, RunOutcome
    from buildlimits.outcome_segment_inventory import Inventory

type RunStatus = typ.Literal[
    "ok", "failed", "corrupt", "busy", "no_headroom", "unsettled"
]

__all__ = [
    "RunOptions",
    "RunReport",
    "RunStatus",
    "dry_report",
    "settled_report",
    "unfinished_report",
    "unsettled_report",
]


@dc.dataclass(frozen=True, slots=True)
class RunOptions:
    """The caller-selected settings for one run.

    Attributes
    ----------
    preset : int
        The XZ preset used for new and rewritten archives.
    now : datetime | None
        The UTC wall time to use, or None to read the clock once.
    dry_run : bool
        Whether to plan without changing any file.
    clock : Callable[[], datetime]
        The UTC clock the run reads: for its start when ``now`` is None, and
        for the finish time every report carries.
    on_retry : Callable[[str, int, str], None]
        Receives the operation, attempt number and closed category of every
        reading the run retries; the default discards them.
    metrics : MetricsPort | None
        The bounded event transport an ``archive_run`` record is published
        through, or None when the caller collects no metrics.
    """

    preset: int = 6
    now: dt.datetime | None = None
    dry_run: bool = False
    clock: cabc.Callable[[], dt.datetime] = utc_now
    on_retry: cabc.Callable[[str, int, str], None] = ignore_retry
    metrics: MetricsPort | None = None


@dc.dataclass(frozen=True, slots=True)
class RunReport:
    """The bounded outcome of one archive run or inspection.

    Attributes
    ----------
    status : RunStatus
        The closed run classification.
    started_at : datetime
        When the run began.
    finished_at : datetime
        When the run finished.
    dry_run : bool
        Whether any file was changed.
    active_bytes : int
        Logical bytes in the writer's active file.
    retained_bytes : int
        Logical bytes retained after the run, or estimated for a dry run.
    retained_records : int
        Records in the whole retained history: the closed rows sidecar
        metadata describes plus the rows still in the writer's active file.
        Entries whose metadata is missing contribute nothing here; they are
        counted by ``unmeasured_entries`` instead.
    observed_from : datetime | None
        Earliest retained closed observation, when metadata describes one.
    observed_until : datetime | None
        Latest retained observation, from sidecar metadata or the active
        file, when one was read.
    unmeasured_entries : int
        Effective entries whose record count and span could not be read: an
        archive with no trustworthy sidecar, or an active file that could not
        be decoded.
    budget_bytes : int
        The configured logical byte budget.
    cutoff : datetime | None
        The age cutoff applied.
    plan : PlanSummary | None
        The plan this run executed or would execute.
    actions : tuple[ActionResult, ...]
        The operations performed.
    failures : tuple[tuple[int, str], ...]
        Sequences that could not be processed, with a closed reason.
    gaps : tuple[int, ...]
        Missing sequences at or above the retention floor.
    gaps_truncated : bool
        Whether more sequences are missing than ``gaps`` reports.
    missing_sidecars : tuple[int, ...]
        Archives with no sidecar.
    corrupt_sidecars : tuple[int, ...]
        Archives whose sidecar cannot be trusted.
    orphan_sidecars : tuple[int, ...]
        Sidecars with no archive.
    reason : str | None
        A closed run-level reason when the run did not complete.
    """

    status: RunStatus
    started_at: dt.datetime
    finished_at: dt.datetime
    dry_run: bool = False
    active_bytes: int = 0
    retained_bytes: int = 0
    retained_records: int = 0
    observed_from: dt.datetime | None = None
    observed_until: dt.datetime | None = None
    unmeasured_entries: int = 0
    budget_bytes: int = 0
    cutoff: dt.datetime | None = None
    plan: PlanSummary | None = None
    actions: tuple[ActionResult, ...] = ()
    failures: tuple[tuple[int, str], ...] = ()
    gaps: tuple[int, ...] = ()
    gaps_truncated: bool = False
    missing_sidecars: tuple[int, ...] = ()
    corrupt_sidecars: tuple[int, ...] = ()
    orphan_sidecars: tuple[int, ...] = ()
    reason: str | None = None


def dry_report(
    run: ArchiveRun, active: ActiveHistory, inventory: Inventory
) -> RunReport:
    """Build the report a dry run returns without changing any file.

    The inventory and the active history are the settled pair the plan was
    built from, so the report and the plan describe one version of the store;
    reading the inventory again could count a seal between the two.

    Returns
    -------
    RunReport
        A successful dry-run report carrying the plan.
    """
    observed = observed_history(inventory, active)
    return RunReport(
        status="ok",
        started_at=run.started_at,
        finished_at=run.context.clock(),
        dry_run=True,
        active_bytes=active.bytes,
        retained_bytes=run.plan.retained_bytes,
        retained_records=observed.records,
        observed_from=observed.observed_from,
        observed_until=observed.observed_until,
        unmeasured_entries=observed.unmeasured,
        budget_bytes=run.context.policy.max_bytes,
        cutoff=run.plan.cutoff,
        plan=run.summary,
        gaps=inventory.gaps,
        gaps_truncated=inventory.gaps_truncated,
        missing_sidecars=inventory.missing_sidecars,
        corrupt_sidecars=inventory.corrupt_sidecars,
        orphan_sidecars=inventory.orphan_sidecars,
    )


def unsettled_report(
    started_at: dt.datetime,
    *,
    dry_run: bool = False,
    clock: cabc.Callable[[], dt.datetime] = utc_now,
) -> RunReport:
    """Build a retryable report for a store that never held still.

    The writer can keep sealing across every bounded reading, so no inventory
    and active file describe one version of the store. Reporting a plan or a
    count from any of those pairs would undercount history, so nothing is
    planned, nothing is changed, and the timer retries.

    Returns
    -------
    RunReport
        An ``unsettled`` report with no plan.
    """
    return RunReport(
        status="unsettled",
        started_at=started_at,
        finished_at=clock(),
        dry_run=dry_run,
    )


def unfinished_report(
    started_at: dt.datetime,
    status: RunStatus,
    reason: str,
    *,
    clock: cabc.Callable[[], dt.datetime] = utc_now,
) -> RunReport:
    """Build a report for a run that could not start or complete.

    Returns
    -------
    RunReport
        The closed failure report.
    """
    return RunReport(
        status=status,
        started_at=started_at,
        finished_at=clock(),
        reason=reason,
    )


def settled_report(
    run: ArchiveRun,
    outcome: RunOutcome,
    floor: int,
) -> RunReport:
    """Measure the settled store and build the report that describes it.

    Returns
    -------
    RunReport
        The completed run's report, before it is persisted.
    """
    layout = run.context.layout
    final, final_active, snapshot_settled = outcome_archive_retain.settled_snapshot(
        layout, floor, cutoff=run.context.policy.cutoff(run.started_at)
    )
    if not snapshot_settled:
        # The writer kept sealing across every reading, so the report cannot
        # bind rows to one version of the store. Report it as retryable rather
        # than recording an undercount as success.
        outcome.retention_unsettled = True
    elif _retention_owes_work(final, final_active, run):
        # The writer can append or seal between the last retention pass and
        # this read. The report must not claim the store is within its bounds
        # when the next firing still has history to evict.
        outcome.retention_unsettled = True
    retained = final_active.bytes + final.sealed_bytes + final.archived_logical_bytes
    observed = observed_history(final, final_active)
    return RunReport(
        status=_run_status(outcome),
        started_at=run.started_at,
        finished_at=run.context.clock(),
        active_bytes=final_active.bytes,
        retained_bytes=retained,
        retained_records=observed.records,
        observed_from=observed.observed_from,
        observed_until=observed.observed_until,
        unmeasured_entries=observed.unmeasured,
        budget_bytes=run.context.policy.max_bytes,
        cutoff=run.plan.cutoff,
        plan=run.summary,
        actions=tuple(outcome.actions),
        failures=tuple(outcome.failures),
        gaps=final.gaps,
        gaps_truncated=final.gaps_truncated,
        missing_sidecars=final.missing_sidecars,
        corrupt_sidecars=final.corrupt_sidecars,
        orphan_sidecars=final.orphan_sidecars,
    )


def _retention_owes_work(
    inventory: Inventory, active: ActiveHistory, run: ArchiveRun
) -> bool:
    """Whether the final snapshot still calls for retention work.

    The writer never takes the archive lock, so it can append or seal between
    the last retention pass and this read. A report that claimed success over
    that store would tell the timer the budget is met while the next firing
    still has to trim or delete. Only retention decides this: a source sealed
    just before the read is archival work the next firing picks up, and the
    report already counts its bytes.

    Parameters
    ----------
    inventory : Inventory
        The store as the final snapshot read it.
    active : ActiveHistory
        The active file from the same settled reading, measured against the
        run's cutoff so the plan counts the rows retention keeps.
    run : ArchiveRun
        The run whose policy and start time the plan is built with.

    Returns
    -------
    bool
        True when the plan still evicts history or defers a budget.
    """
    pending = plan_run(inventory, run.context.policy, now=run.started_at, active=active)
    return bool(pending.trims or pending.deletes or pending.retention_deferred)


def _run_status(outcome: RunOutcome) -> RunStatus:
    """Classify a completed pass by the failures it recorded.

    A run that met only a full filesystem is retryable: nothing is corrupt and
    the next timer firing picks the work up once space returns. Any other
    failure needs an operator, so it outranks a space failure in the same run.

    Returns
    -------
    RunStatus
        ``ok``, ``unsettled``, ``no_headroom`` or ``corrupt``.
    """
    if not outcome.failures:
        return "unsettled" if outcome.retention_unsettled else "ok"
    if all(reason == "no_headroom" for _sequence, reason in outcome.failures):
        return "no_headroom"
    return "corrupt"
