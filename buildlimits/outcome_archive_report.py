"""The bounded report shape and status vocabulary one archive run produces.

The report is the command's only user-facing surface: it names no filesystem
path and carries closed action and reason vocabularies. Rendering it and mapping
its status to an exit code are adapters' work, so the model stays a plain value.
The builders that classify an unfinished, dry, unsettled or unmeasurable run
live in ``outcome_archive_report_builders``.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_archive_context import ignore_retry
from buildlimits.utc_text import utc_now

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.metrics import MetricsPort
    from buildlimits.outcome_archive_context import ActionResult
    from buildlimits.outcome_archive_plan import PlanSummary
    from buildlimits.outcome_retention_report import RetainedHistory, RetentionLimits

type RunStatus = typ.Literal[
    "ok", "failed", "corrupt", "busy", "no_headroom", "unsettled"
]

__all__ = [
    "BUSY",
    "NO_HEADROOM",
    "UNREADABLE",
    "Maintenance",
    "RunOptions",
    "RunReport",
    "RunStatus",
    "Unfinished",
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
        The clock for the run's start when ``now`` is None, and each finish.
    on_retry : Callable[[str, int, str], None]
        Receives each retried reading's operation, attempt and closed category.
    metrics : MetricsPort | None
        The transport an ``archive_run`` record publishes through, or None.
    """

    preset: int = 6
    now: dt.datetime | None = None
    dry_run: bool = False
    clock: cabc.Callable[[], dt.datetime] = utc_now
    on_retry: cabc.Callable[[str, int, str], None] = ignore_retry
    metrics: MetricsPort | None = None


@dc.dataclass(frozen=True, slots=True)
class Unfinished:
    """The closed classification of a run that could not start or complete.

    Attributes
    ----------
    status : RunStatus
        The status the report carries.
    reason : str
        The closed run-level reason.
    """

    status: RunStatus
    reason: str


BUSY = Unfinished("busy", "busy")
NO_HEADROOM = Unfinished("no_headroom", "no_headroom")
UNREADABLE = Unfinished("failed", "unreadable")


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
        Logical bytes retained after the run, or the bytes an inspection holds.
    fallback_bytes : int
        The part of ``retained_bytes`` taken from a physical file size when
        an archive's logical size is unknown; zero when all are measured.
    retained_records : int
        Records in the whole retained history: the closed rows sidecar metadata
        describes plus the rows in the active file. Entries without metadata
        contribute nothing; ``unmeasured_entries`` counts them instead.
    observed_from : datetime | None
        Earliest retained closed observation, when metadata describes one.
    observed_until : datetime | None
        Latest retained observation, from metadata or the active file.
    unmeasured_entries : int
        Effective entries whose record count and span could not be read: an
        archive with no trustworthy sidecar, or an undecodable active file.
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
