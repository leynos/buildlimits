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
from buildlimits.outcome_archive_report import Maintenance, RunReport, RunStatus
from buildlimits.outcome_retention_history import retained_history
from buildlimits.outcome_retention_report import RetentionLimits
from buildlimits.utc_text import optional_utc_text, utc_now, utc_text

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_archive_run import ArchiveRun, RunOutcome
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_retention_report import ActiveHistory
    from buildlimits.outcome_segment_inventory import Inventory
    from buildlimits.outcome_segments import SegmentLayout

__all__ = [
    "BUSY",
    "NO_HEADROOM",
    "UNREADABLE",
    "Unfinished",
    "after_work_failure",
    "dry_report",
    "observed",
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
        finished_at=utc_now(),
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
    is_replaced: bool = False,
) -> RunReport:
    """Build the report a dry run returns without changing any file.

    ``retained_bytes`` is the plan's conservative estimate, because a dry run
    is asked what the bound would be after the work. ``retained`` describes
    the same inventory the plan was built from, which a caller needs to tell
    the two apart: re-reading the directory here could report a segment that
    the plan never saw. No state file is installed, so the report says so.

    A writer seal racing the planning reads moves rows into a segment the
    inventory never saw. When the caller detected that replacement, the active
    contribution is left unmeasured, so the totals are a lower bound rather
    than a complete zero-row history, and the inspection reports ``corrupt``
    rather than success.

    Parameters
    ----------
    run : ArchiveRun
        The plan and context this inspection describes.
    active : ActiveHistory
        The active file's measured state.
    inventory : Inventory
        The closed-history inventory the plan was derived from.
    is_replaced : bool
        Whether the writer replaced the active file while the plan's reads ran.

    Returns
    -------
    RunReport
        A dry-run report carrying the plan and the history it planned against.
    """
    records, observed_from, observed_until = observed(inventory)
    return RunReport(
        status="ok" if active.complete and not is_replaced else "corrupt",
        started_at=run.started_at,
        finished_at=utc_now(),
        dry_run=True,
        active_bytes=active.logical_bytes,
        retained_bytes=run.plan.retained_bytes,
        retained_records=records,
        observed_from=observed_from,
        observed_until=observed_until,
        budget_bytes=run.context.policy.max_bytes,
        cutoff=run.plan.cutoff,
        plan=run.summary,
        gaps=inventory.gaps,
        gaps_truncated=inventory.gaps_truncated,
        missing_sidecars=inventory.missing_sidecars,
        corrupt_sidecars=inventory.corrupt_sidecars,
        orphan_sidecars=inventory.orphan_sidecars,
        limits=RetentionLimits.of(run.context.policy),
        retained=retained_history(inventory, None if is_replaced else active),
        maintenance=Maintenance(state_written=False),
    )


def after_work_failure(
    run: ArchiveRun,
    active: ActiveHistory,
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
    active : ActiveHistory
        The active file's measured state.
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
        finished_at=utc_now(),
        active_bytes=active.logical_bytes,
        budget_bytes=run.context.policy.max_bytes,
        cutoff=run.plan.cutoff,
        plan=run.summary,
        actions=tuple(outcome.actions),
        failures=tuple(outcome.failures),
        limits=RetentionLimits.of(run.context.policy),
        maintenance=Maintenance(state_written=False, reason=reason),
        reason=reason,
    )


def observed(
    inventory: Inventory,
) -> tuple[int, dt.datetime | None, dt.datetime | None]:
    """Summarize the closed history metadata describes.

    Parameters
    ----------
    inventory : Inventory
        The closed history to summarize.

    Returns
    -------
    tuple[int, datetime | None, datetime | None]
        Retained record count, earliest and latest observation. Entries
        without trustworthy metadata contribute nothing rather than guessing.
    """
    described = [
        entry for entry in inventory.effective if entry.record_count is not None
    ]
    records = sum(entry.record_count or 0 for entry in described)
    first = [entry.first_observed_at for entry in described if entry.first_observed_at]
    last = [entry.last_observed_at for entry in described if entry.last_observed_at]
    return records, min(first, default=None), max(last, default=None)


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
