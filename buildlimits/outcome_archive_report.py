"""The bounded report and exit statuses one archive run produces.

The report is the command's only user-facing surface: it names no filesystem
path, carries closed action and reason vocabularies, and maps each outcome to
the status a systemd timer records. State files reuse the same document so an
operator can compare a run against the retention record without reading code.
"""

import dataclasses as dc
import typing as typ

from buildlimits.utc_text import optional_utc_text, utc_text

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_archive_context import ActionResult
    from buildlimits.outcome_archive_plan import PlanSummary

ARCHIVE_SCHEMA = 1
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CORRUPT = 3
EXIT_TEMPFAIL = 75

type RunStatus = typ.Literal[
    "ok", "failed", "corrupt", "busy", "no_headroom", "unsettled"
]

__all__ = [
    "ARCHIVE_SCHEMA",
    "EXIT_CORRUPT",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_TEMPFAIL",
    "EXIT_USAGE",
    "RunOptions",
    "RunReport",
    "RunStatus",
    "action_dict",
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
    """

    preset: int = 6
    now: dt.datetime | None = None
    dry_run: bool = False


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

    @property
    def exit_status(self) -> int:
        """The process status a timer should record.

        Returns
        -------
        int
            Zero for success, one for a failed run, two for usage, three for
            corruption, and 75 when a retry may succeed.
        """
        if self.status in {"busy", "no_headroom", "unsettled"}:
            return EXIT_TEMPFAIL
        if self.status == "corrupt":
            return EXIT_CORRUPT
        if self.status == "failed":
            return EXIT_FAILED
        return EXIT_OK

    def as_dict(self) -> dict[str, object]:
        """Return the stable JSON fields a report or state file carries.

        Returns
        -------
        dict[str, object]
            A JSON-compatible mapping with no filesystem paths.
        """
        return {
            "schema": ARCHIVE_SCHEMA,
            "status": self.status,
            "dry_run": self.dry_run,
            "started_at": utc_text(self.started_at),
            "finished_at": utc_text(self.finished_at),
            "active_bytes": self.active_bytes,
            "retained_logical_bytes": self.retained_bytes,
            "retained_records": self.retained_records,
            "observed_from": optional_utc_text(self.observed_from),
            "observed_until": optional_utc_text(self.observed_until),
            "unmeasured_entries": self.unmeasured_entries,
            "budget_bytes": self.budget_bytes,
            "cutoff": optional_utc_text(self.cutoff),
            "plan": None if self.plan is None else self.plan.as_dict(),
            "actions": [action_dict(action) for action in self.actions],
            "failures": [
                {"sequence": sequence, "reason": reason}
                for sequence, reason in self.failures
            ],
            "gaps": list(self.gaps),
            "gaps_truncated": self.gaps_truncated,
            "missing_sidecars": list(self.missing_sidecars),
            "corrupt_sidecars": list(self.corrupt_sidecars),
            "orphan_sidecars": list(self.orphan_sidecars),
            "reason": self.reason,
        }


def action_dict(action: ActionResult) -> dict[str, object]:
    """Render one performed action for the report.

    Parameters
    ----------
    action : ActionResult
        One operation the run performed.

    Returns
    -------
    dict[str, object]
        The action's closed name, sequence, sizes and optional reason.
    """
    return {
        "action": action.action,
        "sequence": action.sequence,
        "logical_bytes": action.logical_bytes,
        "physical_bytes": action.physical_bytes,
        "reason": action.reason,
    }
