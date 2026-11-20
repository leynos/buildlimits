"""The bounded report and status vocabulary one archive run produces.

The report is the command's only user-facing surface: it names no filesystem
path and carries closed action and reason vocabularies. Rendering it and
mapping its status to a process exit code are adapters' work, so the model
stays a plain value that a caller can compare and pass on.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_archive_context import ignore_retry
from buildlimits.outcome_archive_history import (
    ActiveHistory,
    observed_history,
    physical_fallback_bytes,
    retained_logical_bytes,
)
from buildlimits.outcome_retention_history import retained_history
from buildlimits.outcome_retention_report import RetentionLimits
from buildlimits.utc_text import utc_now

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.metrics import MetricsPort
    from buildlimits.outcome_archive_context import ActionResult
    from buildlimits.outcome_archive_history import ActiveHistory, SettledStore
    from buildlimits.outcome_archive_plan import PlanSummary
    from buildlimits.outcome_archive_run import ArchiveRun, RunOutcome
    from buildlimits.outcome_retention_report import RetainedHistory
    from buildlimits.outcome_segment_inventory import Inventory

# The closed vocabulary a report counts evictions under. A trim that serves
# more than one bound names each of them, so every combination is listed.
EVICTION_CAUSES = (
    "age",
    "bytes",
    "records",
    "age_and_bytes",
    "age_and_records",
    "bytes_and_records",
    "age_and_bytes_and_records",
)

type RunStatus = typ.Literal[
    "ok", "failed", "corrupt", "busy", "no_headroom", "unsettled"
]

__all__ = [
    "EVICTION_CAUSES",
    "Maintenance",
    "RunOptions",
    "RunReport",
    "RunStatus",
    "after_work_failure",
    "dry_report",
    "eviction_counts",
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
class Maintenance:
    """The state a run left behind after its plan had executed.

    Attributes
    ----------
    state_written : bool
        Whether ``retention.json`` and ``last-run.json`` were installed.
    reason : str | None
        A closed reason when state persistence failed.
    """

    state_written: bool = True
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the stable JSON fields for maintenance state.

        Returns
        -------
        dict[str, object]
            Whether state was persisted and why it was not.
        """
        return {"state_written": self.state_written, "reason": self.reason}


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
        Logical bytes retained after the run, or the bytes an inspection still
        holds.
    fallback_bytes : int
        The part of ``retained_bytes`` taken from a physical file size because
        an archive's logical size is unknown; zero when every entry is
        measured.
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
    limits : RetentionLimits | None
        The effective retention bounds, or None when none was derived.
    retained : RetainedHistory | None
        What the store retains after the run, or after the inspected state.
    maintenance : Maintenance
        Whether the run persisted its state.
    reason : str | None
        A closed run-level reason when the run did not complete.
    """

    status: RunStatus
    started_at: dt.datetime
    finished_at: dt.datetime
    dry_run: bool = False
    active_bytes: int = 0
    retained_bytes: int = 0
    fallback_bytes: int = 0
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
    limits: RetentionLimits | None = None
    retained: RetainedHistory | None = None
    maintenance: Maintenance = dc.field(default_factory=Maintenance)
    reason: str | None = None


def dry_report(
    run: ArchiveRun,
    active: ActiveHistory,
    inventory: Inventory,
) -> RunReport:
    """Build the report a dry run returns without changing any file.

    The inventory and the active history are the settled pair the plan was
    built from, so the report and the plan describe one version of the store;
    reading the inventory again could count a seal between the two. An active
    file that could not be counted makes the retained totals a lower bound, so
    the inspection reports ``corrupt`` rather than claiming the history it
    could not read is empty. No state file is installed, and the report says
    so.

    Returns
    -------
    RunReport
        A dry-run report carrying the plan and the history it planned against.
    """
    observed = observed_history(inventory, active)
    return RunReport(
        status="ok" if active.has_metadata else "corrupt",
        started_at=run.started_at,
        finished_at=run.context.clock(),
        dry_run=True,
        active_bytes=active.bytes,
        retained_bytes=retained_logical_bytes(inventory, active),
        fallback_bytes=physical_fallback_bytes(inventory),
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
        limits=RetentionLimits.of(run.context.policy),
        retained=retained_history(inventory, active),
        maintenance=Maintenance(state_written=False),
    )


def unsettled_report(
    started_at: dt.datetime,
    *,
    dry_run: bool = False,
    clock: cabc.Callable[[], dt.datetime] = utc_now,
    limits: RetentionLimits | None = None,
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
        limits=limits,
        maintenance=Maintenance(state_written=False, reason="unsettled"),
    )


def unfinished_report(
    started_at: dt.datetime,
    status: RunStatus,
    reason: str,
    *,
    clock: cabc.Callable[[], dt.datetime] = utc_now,
    limits: RetentionLimits | None = None,
    dry_run: bool = False,
) -> RunReport:
    """Build a report for a run that could not start or complete.

    The effective limits are reported even when no run happened, so a caller
    that only reads the failure document still learns the policy in force. The
    dry-run marker is carried too: an inspection that could not start changed
    no file, and its report must not read as a run that did.

    Returns
    -------
    RunReport
        The closed failure report.
    """
    return RunReport(
        status=status,
        started_at=started_at,
        finished_at=clock(),
        dry_run=dry_run,
        budget_bytes=0 if limits is None else limits.max_bytes,
        limits=limits,
        maintenance=Maintenance(state_written=False, reason=reason),
        reason=reason,
    )


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
    retained = retained_logical_bytes(inventory, active)
    observed = observed_history(inventory, active)
    return RunReport(
        status=_run_status(
            outcome,
            active,
            retention_unsettled=settled.retention_unsettled,
        ),
        started_at=run.started_at,
        finished_at=run.context.clock(),
        active_bytes=active.bytes,
        retained_bytes=retained,
        fallback_bytes=physical_fallback_bytes(inventory),
        retained_records=observed.records,
        observed_from=observed.observed_from,
        observed_until=observed.observed_until,
        unmeasured_entries=observed.unmeasured,
        budget_bytes=run.context.policy.max_bytes,
        cutoff=run.plan.cutoff,
        plan=run.summary,
        actions=tuple(outcome.actions),
        failures=tuple(outcome.failures),
        gaps=inventory.gaps,
        gaps_truncated=inventory.gaps_truncated,
        missing_sidecars=inventory.missing_sidecars,
        corrupt_sidecars=inventory.corrupt_sidecars,
        orphan_sidecars=inventory.orphan_sidecars,
        limits=RetentionLimits.of(run.context.policy),
        retained=retained_history(inventory, active),
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
    see.

    Returns
    -------
    RunStatus
        ``ok``, ``unsettled``, ``no_headroom`` or ``corrupt``.
    """
    if not outcome.failures and not active.has_metadata:
        return "corrupt"
    if not outcome.failures:
        return "unsettled" if retention_unsettled else "ok"
    if all(reason == "no_headroom" for _sequence, reason in outcome.failures):
        return "no_headroom"
    return "corrupt"


def eviction_counts(actions: tuple[ActionResult, ...]) -> dict[str, int]:
    """Count the trims and deletions by the bound that caused them.

    Parameters
    ----------
    actions : tuple[ActionResult, ...]
        The operations a run performed.

    Returns
    -------
    dict[str, int]
        Closed counts keyed by eviction cause, always present so a reader can
        compare two runs without checking whether a key exists.

    Examples
    --------
    >>> counts = eviction_counts(())
    >>> {cause: count for cause, count in counts.items() if count}
    {}
    >>> list(counts) == list(EVICTION_CAUSES)
    True
    """
    counts = dict.fromkeys(EVICTION_CAUSES, 0)
    for action in actions:
        reason = _eviction_cause(action)
        if reason is not None and reason in counts:
            counts[reason] += 1
    return counts


def _eviction_cause(action: ActionResult) -> str | None:
    """Return the eviction cause an action names, if it is an eviction.

    Returns
    -------
    str | None
        The closed cause for a trim or deletion, or None for every other
        action and for a reason that is not a cause.
    """
    reason = action.reason
    if action.action in {"trimmed", "deleted"} and isinstance(reason, str):
        return reason
    return None


def after_work_failure(
    run: ArchiveRun,
    outcome: RunOutcome,
    reason: str,
) -> RunReport:
    """Report a run whose result could not be measured after it did the work.

    The archives and evictions are already published, so the actions and
    failures are reported as they happened. The retained totals are unknown,
    and no state file was installed; a retry recomputes both from the disk.

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
    RunReport
        A failed report that still records the work the run completed.
    """
    return RunReport(
        status="failed",
        started_at=run.started_at,
        finished_at=run.context.clock(),
        budget_bytes=run.context.policy.max_bytes,
        cutoff=run.plan.cutoff,
        plan=run.summary,
        actions=tuple(outcome.actions),
        failures=tuple(outcome.failures),
        limits=RetentionLimits.of(run.context.policy),
        maintenance=Maintenance(state_written=False, reason=reason),
        reason=reason,
    )
