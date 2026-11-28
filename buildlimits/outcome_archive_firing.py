"""The clock decision and state persistence one maintenance firing performs.

Every firing reads the wall clock and the boot clock together, compares them
with the baseline the last locked firing recorded, and records its own reading
once it has finished. Keeping that sequence here leaves the command module to
choose entry points and map faults, and gives both operations one definition of
when age eviction is withheld.
"""

import contextlib
import dataclasses as dc
import typing as typ

from buildlimits import (
    outcome_archive_clock_guard as clock_guard,
)
from buildlimits import (
    outcome_archive_clock_state as clock_state,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_archive_report import RunOptions, RunReport
    from buildlimits.outcome_segments import SegmentLayout

__all__ = ["Guarded", "guarded", "record", "record_on_failure"]


@dc.dataclass(frozen=True, slots=True)
class Guarded:
    """The context one firing plans with, and the reading it must record.

    Attributes
    ----------
    context : ArchiveContext
        The context carrying the clock decision and, for a locked run, the
        suppression the decision applies.
    reading : ClockReading
        The reading recorded after a locked run finishes.
    """

    context: ArchiveContext
    reading: clock_guard.ClockReading


def guarded(
    context: ArchiveContext, selected: RunOptions, started: dt.datetime
) -> Guarded:
    """Decide whether this firing's clock reading can be trusted.

    The decision is made before the lock so every report the firing builds can
    describe it, including one that never started. A dry run reports the
    decision but does not act on it: it changes no state, so withholding a trim
    from its plan would describe a run that will not happen.

    Returns
    -------
    Guarded
        The context carrying the decision, and the reading to record after a
        locked run.
    """
    reading = clock_guard.observe(
        wall=started,
        boot_id=selected.boot_id(),
        boottime=selected.boottime(),
    )
    baseline = clock_state.read_baseline(context.layout)
    skew = clock_guard.is_skewed(baseline.reading, reading)
    return Guarded(
        context=dc.replace(
            context,
            clock_skew=skew,
            suppress_age=skew and not selected.dry_run,
            clock_baseline_unreadable=baseline.state == "unreadable",
        ),
        reading=reading,
    )


def record(
    layout: SegmentLayout,
    reading: clock_guard.ClockReading,
    report: RunReport,
) -> RunReport:
    """Record one firing's clock baseline, reporting a failed write.

    A baseline that cannot be written leaves the store intact, but the next
    firing may then withhold age eviction once more than necessary. The
    failure is named rather than hidden, and an existing run-level reason is
    kept because the fault that stopped the run is the more useful one.

    Returns
    -------
    RunReport
        The report, with a failed baseline write recorded when it is the only
        maintenance fault.
    """
    try:
        clock_state.write_baseline(layout, reading)
    except OSError:
        maintenance = dc.replace(report.maintenance, baseline_written=False)
        if report.reason is not None:
            # The fault that stopped the run is the more useful reason, and the
            # state flag belongs to that fault; only the baseline marker is
            # added.
            return dc.replace(report, maintenance=maintenance)
        return dc.replace(
            report,
            status="failed",
            reason="clock_baseline_failed",
            maintenance=dc.replace(maintenance, reason="clock_baseline_failed"),
        )
    return report


@contextlib.contextmanager
def record_on_failure(
    layout: SegmentLayout, reading: clock_guard.ClockReading
) -> cabc.Generator[None]:
    """Record the baseline when the locked run raises before it reports.

    The report the caller builds afterwards names the fault that stopped the
    run, so this second write has no result to modify. Failing to record it
    leaves the store intact and can only make the next firing more cautious.
    """
    try:
        yield
    except BaseException:
        with contextlib.suppress(OSError):
            clock_state.write_baseline(layout, reading)
        raise
