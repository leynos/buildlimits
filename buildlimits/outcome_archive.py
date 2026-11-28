"""Maintain closed cache-outcome history under the configured policy.

An ``archive`` run plans from a content-free inventory, takes the archive lock,
compresses and verifies one sealed source at a time, then applies the age and
logical byte budget by trimming or deleting the oldest closed history. A
``retain`` run applies the same policy without archiving sealed sources: a
sealed straddler is rewritten in place, while an existing archive is
recompressed or removed whole. The active file is never opened for writing:
only the writer seals it. Every failure is reported as a closed reason and an
exit status a systemd timer can act on.

This module chooses the operation, decides whether its clock reading can be
trusted, and maps every fault to a closed report. Planning and execution under
the lock live in ``outcome_archive_execute``.
"""

import dataclasses as dc
import typing as typ

from buildlimits import (
    outcome_archive_events,
    outcome_archive_evict,
    outcome_archive_execute,
    outcome_archive_firing,
    outcome_archive_lock,
)
from buildlimits.outcome_archive_budget import UnknownActiveHistoryError
from buildlimits.outcome_archive_context import ArchiveContext
from buildlimits.outcome_archive_quarantine import fresh_quarantine_token
from buildlimits.outcome_archive_report import (
    BUSY,
    NO_HEADROOM,
    UNREADABLE,
    RunOptions,
    RunReport,
    RunStatus,
    Unfinished,
)
from buildlimits.outcome_archive_report_builders import unfinished_report

if typ.TYPE_CHECKING:
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segments import SegmentLayout

__all__ = [
    "RunOptions",
    "RunReport",
    "RunStatus",
    "inspect_store",
    "run_archive",
    "run_retain",
]


def inspect_store(
    layout: SegmentLayout,
    policy: RetentionPolicy,
    *,
    options: RunOptions | None = None,
) -> RunReport:
    """Report the store's history and the archive plan a run would execute.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    policy : RetentionPolicy
        The effective retention policy.
    options : RunOptions | None
        The clocks, retry observer and metrics port the inspection uses. The
        dry-run flag is forced on, because an inspection never changes a file.

    Returns
    -------
    RunReport
        A dry-run report; no file is created or changed.
    """
    selected = RunOptions() if options is None else options
    return _run(layout, policy, dc.replace(selected, dry_run=True))


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
    return _run(layout, policy, RunOptions() if options is None else options)


def run_retain(
    layout: SegmentLayout,
    policy: RetentionPolicy,
    options: RunOptions | None = None,
) -> RunReport:
    """Plan and optionally execute one retention-only run.

    The operation shares the policy, lock, settled snapshot, floor and exit
    codes with an archive run. It never archives a sealed source, so a timer
    can enforce the bounds without paying to compress history that has not
    been archived yet. An existing archive that straddles a bound is still
    recompressed, because keeping it compressed is the published contract.

    Returns
    -------
    RunReport
        What the run did, or would do, and its bounded exit status.
    """
    selected = RunOptions() if options is None else options
    return _run(layout, policy, dc.replace(selected, operation="retain"))


def _run(
    layout: SegmentLayout,
    policy: RetentionPolicy,
    selected: RunOptions,
) -> RunReport:
    """Run or inspect one operation under the caller's options.

    Returns
    -------
    RunReport
        The run's report, whatever the store did.
    """
    started = selected.clock() if selected.now is None else selected.now
    context = ArchiveContext(
        layout=layout,
        policy=policy,
        preset=selected.preset,
        now=started,
        operation=selected.operation,
        clock=selected.clock,
        on_retry=selected.on_retry,
        quarantine_token=fresh_quarantine_token,
    )
    guarded = outcome_archive_firing.guarded(context, selected, started)
    report = _attempt_run(guarded.context, selected)
    if not selected.dry_run:
        # An inspection is a query: it reads the store and reports, but it
        # does not publish an execution event.
        outcome_archive_events.publish_run(selected.metrics, report)
    return report


def _attempt_run(context: ArchiveContext, selected: RunOptions) -> RunReport:
    """Run or inspect the store, mapping every fault to a closed report.

    Returns
    -------
    RunReport
        The run's report, whatever the store did.
    """
    started = context.now
    try:
        if selected.dry_run:
            # A dry run mutates nothing, so it may plan without the lock and
            # records no clock baseline.
            return outcome_archive_execute.inspect(context, started)
        with outcome_archive_lock.ArchiveLock(context.layout.lock_file):
            # Another firing may have held the lock while this one waited, and
            # recorded a newer baseline. The decision and the reading are
            # therefore taken again now that the store cannot move underneath
            # them; the pre-lock decision still describes a run that never
            # acquired the lock.
            locked = outcome_archive_firing.guarded(context, selected, started)
            with outcome_archive_firing.record_on_failure(
                locked.context.layout, locked.reading
            ):
                return outcome_archive_execute.execute_locked(
                    locked.context, started, locked.reading
                )
    except outcome_archive_lock.ArchiveBusyError:
        return unfinished_report(started, BUSY, context, dry_run=selected.dry_run)
    except outcome_archive_lock.NoHeadroomError:
        return unfinished_report(
            started, NO_HEADROOM, context, dry_run=selected.dry_run
        )
    except UnknownActiveHistoryError:
        # The record cap counts the writer's unsealed rows. A run that cannot
        # read them cannot show the store is within the cap, so it reports an
        # operational failure rather than counting the active file as empty.
        return unfinished_report(started, UNREADABLE, context, dry_run=selected.dry_run)
    except OSError as error:
        # The headroom check reads free space once, before the pass, so the
        # filesystem can still fill while this run works. A full filesystem is
        # retryable; anything else needs an operator.
        reason = outcome_archive_evict.failure_reason(error)
        status: RunStatus = "no_headroom" if reason == "no_headroom" else "failed"
        return unfinished_report(
            started,
            Unfinished(status, reason),
            context,
            dry_run=selected.dry_run,
        )
