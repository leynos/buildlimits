"""Build the report and state documents one archive run returns.

Report construction is kept apart from the run's file operations so the
archive module reads as the sequence of steps a run takes. Every field a
caller, a timer or a later inspection reads is assembled here, including the
documents written beside the archives.
"""

import contextlib
import dataclasses as dc
import typing as typ

from buildlimits import outcome_archive_fs
from buildlimits.outcome_archive_history import ActiveHistory, observed_history
from buildlimits.outcome_archive_report import Maintenance, RunReport, RunStatus
from buildlimits.outcome_retention_history import retained_history
from buildlimits.outcome_retention_report import RetentionLimits
from buildlimits.utc_text import optional_utc_text, utc_now, utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_archive_run import ArchiveRun, RunOutcome
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segment_inventory import Inventory
    from buildlimits.outcome_segments import SegmentLayout

__all__ = [
    "BUSY",
    "NO_HEADROOM",
    "UNREADABLE",
    "Unfinished",
    "after_work_failure",
    "dry_report",
    "unfinished",
    "write_floor",
    "write_floor_after_failure",
    "write_state",
]

_STATE_SCHEMA = 1


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


def unfinished(
    started: dt.datetime,
    fault: Unfinished,
    policy: RetentionPolicy,
    *,
    dry_run: bool,
    clock: cabc.Callable[[], dt.datetime] = utc_now,
) -> RunReport:
    """Build a report for a run that could not start or complete.

    Parameters
    ----------
    started : datetime
        When the caller asked for the run.
    fault : Unfinished
        The closed status and reason of the fault.
    policy : RetentionPolicy
        The effective retention policy.
    dry_run : bool
        Whether the caller asked only to inspect the store.
    clock : Callable[[], datetime]
        The UTC clock the report's finish time is read from.

    Returns
    -------
    RunReport
        The closed failure report. The effective limits are reported even when
        no run happened, so a caller that only reads the failure document still
        learns the policy in force. The configured byte budget is reported too,
        so a deferred run cannot read as a zero-byte budget. No maintenance
        state was installed on any of these paths, so the report says so rather
        than inheriting the successful default.
    """
    return RunReport(
        status=fault.status,
        started_at=started,
        finished_at=clock(),
        dry_run=dry_run,
        budget_bytes=policy.max_bytes,
        limits=RetentionLimits.of(policy),
        maintenance=Maintenance(state_written=False, reason=fault.reason),
        reason=fault.reason,
    )


def dry_report(
    run: ArchiveRun,
    active: ActiveHistory,
    inventory: Inventory,
    *,
    clock: cabc.Callable[[], dt.datetime] = utc_now,
) -> RunReport:
    """Build the report a dry run returns without changing any file.

    ``retained_bytes`` is the plan's conservative estimate, because a dry run
    is asked what the bound would be after the work. ``retained`` describes
    the same inventory the plan was built from, which a caller needs to tell
    the two apart: re-reading the directory here could report a segment that
    the plan never saw. An active file that could not be counted makes the
    totals a lower bound, so the inspection reports ``corrupt`` rather than
    presenting the history it could not read as empty. No state file is
    installed, and the report says so.

    Parameters
    ----------
    run : ArchiveRun
        The plan and context this inspection describes.
    active : ActiveHistory
        The active file's measured state.
    inventory : Inventory
        The closed-history inventory the plan was derived from.
    clock : Callable[[], datetime]
        The UTC clock the report's finish time is read from.

    Returns
    -------
    RunReport
        A dry-run report carrying the plan and the history it planned against.
    """
    observed = observed_history(inventory, active)
    return RunReport(
        status="ok" if active.has_metadata else "corrupt",
        started_at=run.started_at,
        finished_at=clock(),
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
        limits=RetentionLimits.of(run.context.policy),
        retained=retained_history(inventory, active),
        maintenance=Maintenance(state_written=False),
    )


def after_work_failure(
    run: ArchiveRun,
    outcome: RunOutcome,
    reason: str,
    *,
    clock: cabc.Callable[[], dt.datetime] = utc_now,
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
    clock : Callable[[], datetime]
        The UTC clock the report's finish time is read from.

    Returns
    -------
    RunReport
        A failed report that still records the work the run completed.
    """
    return RunReport(
        status="failed",
        started_at=run.started_at,
        finished_at=clock(),
        budget_bytes=run.context.policy.max_bytes,
        cutoff=run.plan.cutoff,
        plan=run.summary,
        actions=tuple(outcome.actions),
        failures=tuple(outcome.failures),
        limits=RetentionLimits.of(run.context.policy),
        maintenance=Maintenance(state_written=False, reason=reason),
        reason=reason,
    )


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
    outcome_archive_fs.atomic_write_json(layout.last_run_file, report.as_dict())


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
        layout.retention_file,
        {
            "schema": _STATE_SCHEMA,
            "floor_sequence": floor,
            "retained_logical_bytes": report.retained_bytes,
            "budget_bytes": report.budget_bytes,
            "cutoff": optional_utc_text(report.cutoff),
            "limits": None if report.limits is None else report.limits.as_dict(),
            "retained": None if report.retained is None else report.retained.as_dict(),
            "updated_at": utc_text(report.finished_at),
        },
    )


def write_floor_after_failure(
    layout: SegmentLayout, floor: int, report: RunReport
) -> None:
    """Give the retention floor a second attempt after the state write failed.

    The retention pass has already deleted every sequence the floor advances
    past, and a later run that performs no deletion derives its floor from
    this document alone. A failure of the full state write therefore gets one
    more attempt at the floor, so a transient fault does not turn completed
    deletions into reported gaps. A second failure is not raised: the caller
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
    with contextlib.suppress(OSError):
        write_floor(layout, floor, report)
