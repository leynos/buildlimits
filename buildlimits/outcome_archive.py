"""Archive closed cache-outcome history into verified XZ segments.

A run reads the closed-history inventory together with the active file's size
and rows, takes the archive lock, compresses and verifies one sealed source at
a time, then applies the age, logical byte and record bounds by trimming or
deleting the oldest closed history. The active file is never opened for
writing: only the writer seals it. Its rows are read once, boundedly, to
measure both bounds and to report the retained history. Every failure is
reported as a closed reason and an exit status a systemd timer can act on.
"""

import dataclasses as dc
import datetime as dt
import typing as typ

from buildlimits import (
    outcome_archive_active,
    outcome_archive_fs,
    outcome_archive_lock,
    outcome_archive_reports,
    outcome_archive_run,
    outcome_archive_state,
    outcome_segment_inventory,
    outcome_segment_state,
)
from buildlimits.outcome_archive_context import ArchiveContext
from buildlimits.outcome_archive_plan import PlanSummary, plan_run
from buildlimits.outcome_archive_report import (
    ARCHIVE_SCHEMA,
    EXIT_CORRUPT,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_TEMPFAIL,
    EXIT_USAGE,
    Maintenance,
    RunOptions,
    RunReport,
    RunStatus,
)
from buildlimits.outcome_retention_history import retained_history
from buildlimits.outcome_retention_report import RetentionLimits
from buildlimits.utc_text import utc_now

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_plan import Plan
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_retention_report import ActiveHistory
    from buildlimits.outcome_segments import SegmentLayout

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
            plan, view = _plan(layout, policy, started)
            return outcome_archive_reports.dry_report(
                _archive_run(context, plan),
                view.active,
                view.inventory,
                is_replaced=view.is_replaced,
            )
        with outcome_archive_lock.ArchiveLock(layout.lock_file):
            # Another archiver may have changed the store while this process
            # waited for the lock, so the plan is built from what is on disk
            # now rather than from a pre-lock inventory.
            plan, view = _plan(layout, policy, started)
            return _execute(_archive_run(context, plan), view.active)
    except outcome_archive_lock.ArchiveBusyError:
        return outcome_archive_reports.unfinished(
            started, outcome_archive_reports.BUSY, policy, dry_run=selected.dry_run
        )
    except outcome_archive_lock.NoHeadroomError:
        return outcome_archive_reports.unfinished(
            started,
            outcome_archive_reports.NO_HEADROOM,
            policy,
            dry_run=selected.dry_run,
        )
    except OSError:
        return outcome_archive_reports.unfinished(
            started,
            outcome_archive_reports.UNREADABLE,
            policy,
            dry_run=selected.dry_run,
        )


def _plan(
    layout: SegmentLayout, policy: RetentionPolicy, now: dt.datetime
) -> tuple[Plan, _StoreView]:
    """Read the store as it is and plan one run from it.

    The active file is read once, through a bounded reader, so its size and
    row count drive the plan and its rows describe the retained history
    without a second pass.
    The inventory the plan was built from is returned as well, so a dry run
    reports the same history it planned against rather than a later scan that
    another process may already have changed.

    The writer seals without the archive lock, so both reads are bracketed by
    the active file's identity. A seal in that window moves rows into a
    segment the inventory never saw, and leaves that observation complete
    only if the file did not change. A replaced file leaves the view marked so
    a dry run reports its totals as a lower bound.

    Returns
    -------
    tuple[Plan, _StoreView]
        The operations to perform and the store state the plan was derived
        from.
    """
    floor = outcome_segment_state.read_retention_floor(layout)
    view = _read_store(layout, floor)
    plan = plan_run(view.inventory, policy, now=now, active=view.active)
    return plan, view


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


def _execute(run: outcome_archive_run.ArchiveRun, active: ActiveHistory) -> RunReport:
    """Take the archive directories, perform the plan and write state.

    A run whose active file cannot be counted, or was replaced while it was
    read, is reported as ``corrupt`` even when every closed segment succeeded:
    the retained total is then a lower bound, and a timer that recorded ``ok``
    would hide history it cannot see.

    A state write that fails does not discard the report of work already done:
    the archive and its evictions are real even when ``retention.json`` could
    not be replaced. The run then reports ``failed`` with
    ``state_write_failed``, so a timer retries and the next run rewrites the
    state from what is on disk.

    The floor gets its own attempt before that failure is returned. A later
    run that deletes nothing can only learn about this run's deletions from
    the recorded floor, so a transient failure of the full write must not
    turn them into permanent gaps. A post-run read that fails takes the same
    path: the deletions are already real, so the floor is written from their
    actions without needing another inventory read. A retry that still cannot
    replace ``retention.json`` falls back to ``retention-pending.json``, which
    every reader merges with the primary record; only when both records refuse
    the write is the loss reported as a gap.

    The retained totals come from ``_measure``, which reads each side of the
    store once and detects a seal racing those reads. A replacement in the
    window leaves the active contribution unmeasured, which marks the totals
    incomplete rather than counting the sealed rows twice.

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
    try:
        # The retention pass re-plans from the store this run has just
        # published, so it can fail to read what ``perform`` already wrote.
        outcome_archive_run.retain(run, outcome)
        floor, view = _measure(layout, outcome)
    except OSError:
        # The archives and evictions are already published. Returning the
        # pre-run failure report would erase them from the record, so keep
        # what the run did and say only that the result could not be measured.
        outcome_archive_state.write_deleted_floor(
            layout,
            outcome_segment_state.read_retention_floor(layout),
            outcome_archive_run.deleted_sequences(outcome.actions),
        )
        return outcome_archive_reports.after_work_failure(
            run, active, outcome, "unreadable"
        )
    measured = retained_history(
        view.inventory, None if view.is_replaced else view.active
    )
    records, observed_from, observed_until = outcome_archive_reports.observed(
        view.inventory
    )
    report = RunReport(
        status=(
            "corrupt"
            if outcome.failures or view.is_replaced or not view.active.complete
            else "ok"
        ),
        started_at=run.started_at,
        finished_at=utc_now(),
        active_bytes=view.active.logical_bytes,
        retained_bytes=measured.logical_bytes,
        retained_records=records,
        observed_from=observed_from,
        observed_until=observed_until,
        budget_bytes=run.context.policy.max_bytes,
        cutoff=run.plan.cutoff,
        plan=run.summary,
        actions=tuple(outcome.actions),
        failures=tuple(outcome.failures),
        gaps=view.inventory.gaps,
        gaps_truncated=view.inventory.gaps_truncated,
        missing_sidecars=view.inventory.missing_sidecars,
        corrupt_sidecars=view.inventory.corrupt_sidecars,
        orphan_sidecars=view.inventory.orphan_sidecars,
        limits=RetentionLimits.of(run.context.policy),
        retained=measured,
    )
    try:
        outcome_archive_state.write_state(layout, floor, report)
    except OSError:
        outcome_archive_state.write_floor_after_failure(layout, floor, report)
        return dc.replace(
            report,
            status="failed",
            reason="state_write_failed",
            maintenance=Maintenance(state_written=False, reason="state_write_failed"),
        )
    return report


@dc.dataclass(frozen=True, slots=True)
class _StoreView:
    """Both sides of the store, read once each.

    Attributes
    ----------
    inventory : Inventory
        The closed history as the read observed it.
    active : ActiveHistory
        The active file as the read observed it.
    is_replaced : bool
        Whether the writer installed a different active file during the read.
    """

    inventory: outcome_segment_inventory.Inventory
    active: ActiveHistory
    is_replaced: bool


def _read_store(layout: SegmentLayout, floor: int | None) -> _StoreView:
    """Read both sides of the store and detect a seal racing the reads.

    The writer may seal the active file at any moment without taking the
    archive lock, so the file's identity is compared either side of the reads.
    A replacement in that window means the closed history and the active file
    describe different stores: the caller must report a lower bound instead of
    counting a sealed segment's rows a second time, or presenting a complete
    zero-row history.

    Returns
    -------
    _StoreView
        The closed history and the active file, with whether the writer
        replaced the active file while both were being read.
    """
    before = outcome_archive_active.active_identity(layout.active)
    inventory = outcome_segment_inventory.read_inventory(layout, retention_floor=floor)
    active = outcome_archive_active.read_active_history(layout.active)
    replaced = before != outcome_archive_active.active_identity(layout.active)
    return _StoreView(inventory, active, replaced)


def _measure(
    layout: SegmentLayout, outcome: outcome_archive_run.RunOutcome
) -> tuple[int, _StoreView]:
    """Read the retained history after the run's writes.

    Returns
    -------
    tuple[int, _StoreView]
        The floor to persist and the store as the final read observed it.
    """
    floor = _retention_floor(
        outcome_segment_inventory.read_inventory(layout),
        outcome_segment_state.read_retention_floor(layout),
        outcome,
    )
    return floor, _read_store(layout, floor)


def _retention_floor(
    inventory: outcome_segment_inventory.Inventory,
    previous: int | None,
    outcome: outcome_archive_run.RunOutcome,
) -> int:
    """Return the lowest sequence this history intentionally starts at.

    The floor records what retention removed, not what happens to be absent.
    Deriving it from the surviving sequences would raise it past a sequence
    that went missing for another reason, and the next scan would then stop
    reporting that sequence as a gap. It therefore advances only across a
    contiguous run of deletions from the previous floor: a deletion beyond a
    sequence this run did not remove stops the walk, so the hole stays
    reported. It never falls below the first sequence this store ever had,
    so an empty prefix is not reported as missing history.

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
    baseline = min(present, default=0) if previous is None else previous
    deleted = outcome_archive_run.deleted_sequences(outcome.actions)
    if not deleted:
        return baseline
    return outcome_archive_state.contiguous_floor(max(baseline, 1), deleted)
