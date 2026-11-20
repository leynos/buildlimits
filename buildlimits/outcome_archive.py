"""Archive closed cache-outcome history into verified XZ segments.

A run plans from a content-free inventory, takes the archive lock, compresses
and verifies one sealed source at a time, then applies the age and logical
byte budget by trimming or deleting the oldest closed history. The active file
is never opened for writing: only the writer seals it. Every failure is
reported as a closed reason and an exit status a systemd timer can act on.
"""

import datetime as dt
import typing as typ

from buildlimits import (
    outcome_archive_fs,
    outcome_archive_lock,
    outcome_archive_retain,
    outcome_archive_run,
    outcome_segment_inventory,
    outcome_segment_state,
)
from buildlimits.outcome_archive_budget import UnknownActiveHistoryError
from buildlimits.outcome_archive_context import ArchiveContext
from buildlimits.outcome_archive_history import ActiveHistory, observed_history
from buildlimits.outcome_archive_plan import PlanSummary, plan_run
from buildlimits.outcome_archive_report import (
    ARCHIVE_SCHEMA,
    EXIT_CORRUPT,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_TEMPFAIL,
    EXIT_USAGE,
    RunOptions,
    RunReport,
    RunStatus,
    dry_report,
    unfinished_report,
    unsettled_report,
)
from buildlimits.utc_text import optional_utc_text, utc_now, utc_text

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_plan import Plan
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segment_inventory import Inventory
    from buildlimits.outcome_segments import SegmentLayout

_STATE_SCHEMA = 1

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
    "inspect_store",
    "run_archive",
]


def inspect_store(
    layout: SegmentLayout,
    policy: RetentionPolicy,
    *,
    now: dt.datetime | None = None,
) -> RunReport:
    """Report the store's history and the plan a run would execute.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    policy : RetentionPolicy
        The effective retention policy.
    now : datetime | None
        The UTC wall time to use, or None to read the clock once.

    Returns
    -------
    RunReport
        A dry-run report; no file is created or changed.
    """
    return run_archive(layout, policy, RunOptions(now=now, dry_run=True))


def run_archive(
    layout: SegmentLayout,
    policy: RetentionPolicy,
    options: RunOptions | None = None,
) -> RunReport:
    """Plan and optionally execute one archive run.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    policy : RetentionPolicy
        The effective retention policy.
    options : RunOptions | None
        The caller's preset, time and dry-run choice.

    Returns
    -------
    RunReport
        What the run did, or would do, and its bounded exit status.
    """
    selected = RunOptions() if options is None else options
    started = dt.datetime.now(dt.UTC) if selected.now is None else selected.now
    context = ArchiveContext(
        layout=layout, policy=policy, preset=selected.preset, now=started
    )
    try:
        if selected.dry_run:
            # A dry run mutates nothing, so it may plan without the lock.
            return _inspect(context, policy, started)
        with outcome_archive_lock.ArchiveLock(layout.lock_file):
            return _execute_locked(context, policy, started)
    except outcome_archive_lock.ArchiveBusyError:
        return unfinished_report(started, "busy", "busy")
    except outcome_archive_lock.NoHeadroomError:
        return unfinished_report(started, "no_headroom", "no_headroom")
    except UnknownActiveHistoryError:
        # The record cap counts the writer's unsealed rows. A run that cannot
        # read them cannot show the store is within the cap, so it reports an
        # operational failure rather than counting the active file as empty.
        return unfinished_report(started, "failed", "unreadable")
    except OSError as error:
        # The headroom check reads free space once, before the pass, so the
        # filesystem can still fill while this run works. A full filesystem is
        # retryable; anything else needs an operator.
        reason = outcome_archive_run.failure_reason(error)
        status: RunStatus = "no_headroom" if reason == "no_headroom" else "failed"
        return unfinished_report(started, status, reason)


def _inspect(
    context: ArchiveContext, policy: RetentionPolicy, started: dt.datetime
) -> RunReport:
    """Plan and report one dry run without changing any file.

    Returns
    -------
    RunReport
        The settled dry-run report, or a retryable result when the writer kept
        moving across every reading.
    """
    plan, active, inventory, settled = _plan(context.layout, policy, started)
    if not settled:
        return unsettled_report(started, dry_run=True)
    return dry_report(_archive_run(context, plan), active, inventory)


def _execute_locked(
    context: ArchiveContext, policy: RetentionPolicy, started: dt.datetime
) -> RunReport:
    """Plan and execute one run while its lock is held.

    Another archiver may have changed the store while this process waited for
    the lock, so the plan is built from what is on disk now rather than from a
    pre-lock inventory. A plan built from a torn pair would count neither the
    sealed copy nor its active rows, so nothing is changed and the run reports
    a retryable result instead.

    Returns
    -------
    RunReport
        The completed run's report.
    """
    plan, _active, _inventory, settled = _plan(context.layout, policy, started)
    if not settled:
        return unsettled_report(started)
    return _execute(_archive_run(context, plan))


def _plan(
    layout: SegmentLayout, policy: RetentionPolicy, now: dt.datetime
) -> tuple[Plan, ActiveHistory, Inventory, bool]:
    """Read one consistent store version and plan one run from it.

    The inventory and the active file are read together, retrying a seal that
    lands between them. A plan built from a torn pair would count neither the
    sealed copy nor its active rows, and the same reading supplies a dry-run
    report, so both describe one version. The active file is measured against
    the cutoff so the plan's budgets count only the rows retention keeps, while
    its whole-file figures still describe what the writer holds.

    Returns
    -------
    tuple[Plan, ActiveHistory, Inventory, bool]
        The operations to perform, what the active file held, the inventory
        they were read with, and whether the readings describe one version of
        the store. False means no snapshot settled within the bounded
        attempts, so the caller must report an unsettled result rather than
        act on the plan.
    """
    floor = outcome_segment_state.read_retention_floor(layout)
    inventory, active, settled = outcome_archive_retain.settled_snapshot(
        layout, floor, cutoff=policy.cutoff(now)
    )
    return (
        plan_run(inventory, policy, now=now, active=active),
        active,
        inventory,
        settled,
    )


def _archive_run(context: ArchiveContext, plan: Plan) -> outcome_archive_run.ArchiveRun:
    """Bind one plan to the context its execution shares.

    Returns
    -------
    ArchiveRun
        The immutable inputs of one execution pass.
    """
    return outcome_archive_run.ArchiveRun(
        context=context,
        plan=plan,
        summary=PlanSummary.of(plan),
        started_at=context.now,
    )


def _execute(run: outcome_archive_run.ArchiveRun) -> RunReport:
    """Take the archive directories, perform the plan and write state.

    Returns
    -------
    RunReport
        The completed run's report.
    """
    layout = run.context.layout
    outcome_archive_fs.ensure_private_directory(layout.archive_dir)
    outcome_archive_fs.remove_stale_temps(layout.archive_dir)
    outcome_archive_lock.require_headroom(
        layout.archive_dir, run.context.policy.headroom_bytes
    )
    outcome = outcome_archive_run.RunOutcome()
    outcome_archive_run.perform(run, outcome)
    outcome.retention_unsettled = not outcome_archive_retain.retain(run, outcome)
    floor = _retention_floor(
        outcome_segment_inventory.read_inventory(layout),
        outcome_segment_state.read_retention_floor(layout),
        outcome,
    )
    final, final_active, snapshot_settled = outcome_archive_retain.settled_snapshot(
        layout, floor
    )
    if not snapshot_settled:
        # The writer kept sealing across every reading, so the report cannot
        # bind rows to one version of the store. Report it as retryable rather
        # than recording an undercount as success.
        outcome.retention_unsettled = True
    retained = final_active.bytes + final.sealed_bytes + final.archived_logical_bytes
    observed = observed_history(final, final_active)
    report = RunReport(
        status=_run_status(outcome),
        started_at=run.started_at,
        finished_at=utc_now(),
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
    _write_state(layout, floor, report)
    return report


def _run_status(outcome: outcome_archive_run.RunOutcome) -> RunStatus:
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


def _write_state(layout: SegmentLayout, floor: int, report: RunReport) -> None:
    """Persist the retention floor and the run report durably."""
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


def _retention_floor(
    inventory: outcome_segment_inventory.Inventory,
    previous: int | None,
    outcome: outcome_archive_run.RunOutcome,
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
    previous : int | None
        The floor the last successful run recorded.
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
        previous if previous is not None else min([*present, *deleted], default=0)
    )
    # Only a contiguous deleted prefix advances the floor. A sequence that is
    # missing without a deletion may be a gap retention did not create, and
    # stepping past it would stop the next scan reporting it.
    floor = baseline
    while floor in deleted:
        floor += 1
    return floor
