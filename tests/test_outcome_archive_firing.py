"""The clock decision and baseline one firing records.

The baseline withholds age eviction for one firing, so its own failure must be
reported without discarding the state flags of the report it failed beside. A
report that claims a baseline was written when it was not would send an
operator looking for a file that does not exist.
"""

import datetime as dt
import json
import typing as typ
from pathlib import Path

from outcome_archive_helpers import NOW, layout, policy, refuse

from buildlimits import (
    outcome_archive,
    outcome_archive_clock_guard,
    outcome_archive_clock_state,
    outcome_archive_execute,
    outcome_archive_firing,
    outcome_archive_lock,
)
from buildlimits.outcome_archive import RunOptions, run_retain
from buildlimits.outcome_archive_clock_state import BaselineRead
from buildlimits.outcome_archive_report import Maintenance, RunReport

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_archive_report import RunStatus


def _report(
    *,
    status: RunStatus = "ok",
    reason: str | None = None,
    maintenance: Maintenance | None = None,
) -> RunReport:
    """Build one report at the shared test moment.

    Returns
    -------
    RunReport
        The report the baseline recorder amends.
    """
    return RunReport(
        status=status,
        started_at=NOW,
        finished_at=NOW,
        reason=reason,
        maintenance=Maintenance() if maintenance is None else maintenance,
    )


def _failing_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> outcome_archive_clock_guard.ClockReading:
    """Make every baseline write fail and return the reading to record.

    Returns
    -------
    ClockReading
        The reading the recorder is asked to persist.
    """
    monkeypatch.setattr(outcome_archive_clock_state, "write_baseline", refuse)
    return outcome_archive_clock_guard.observe(wall=NOW, boot_id="boot", boottime=1.0)


def test_a_failed_baseline_write_marks_only_the_baseline_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report's own state flags survive a failed baseline write."""
    store_layout = layout(tmp_path)
    reading = _failing_baseline(monkeypatch)

    settled = outcome_archive_firing.record(store_layout, reading, _report())
    deferred = outcome_archive_firing.record(
        store_layout,
        reading,
        _report(
            status="unsettled",
            maintenance=Maintenance(state_written=False, reason="unsettled"),
        ),
    )
    failed = outcome_archive_firing.record(
        store_layout,
        reading,
        _report(
            status="failed",
            reason="state_write_failed",
            maintenance=Maintenance(state_written=False, reason="state_write_failed"),
        ),
    )

    assert settled.status == "failed", "a lone baseline failure makes the run fail"
    assert settled.reason == "clock_baseline_failed", "the closed reason is named"
    assert settled.maintenance.baseline_written is False, "the flag records the fault"
    assert deferred.maintenance.state_written is False, (
        "a report that installed no state keeps saying so"
    )
    assert deferred.maintenance.baseline_written is False, "the baseline flag is set"
    assert failed.status == "failed", "the primary failure keeps its status"
    assert failed.reason == "state_write_failed", "the primary reason is preserved"
    assert failed.maintenance.reason == "state_write_failed", (
        "the state reason is not replaced by the baseline's"
    )
    assert failed.maintenance.state_written is False, "the state flag is preserved"
    assert failed.maintenance.baseline_written is False, (
        "the baseline flag still records its own failure"
    )


def test_the_persisted_report_names_a_failed_baseline_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report a timer reads describes the baseline it could not record."""
    store_layout = layout(tmp_path)
    _failing_baseline(monkeypatch)

    report = run_retain(store_layout, policy(1_000_000), RunOptions(now=NOW))

    assert report.status == "failed", "the timer retries a failed baseline"
    assert report.reason == "clock_baseline_failed", "the closed reason is returned"
    persisted = json.loads(store_layout.last_retain_file.read_text(encoding="utf-8"))
    assert persisted["status"] == "failed", "the persisted report agrees"
    assert persisted["reason"] == "clock_baseline_failed", (
        "the persisted reason is the baseline failure"
    )
    assert persisted["maintenance"]["baseline_written"] is False, (
        "the persisted document does not claim a baseline it never wrote"
    )


def test_a_post_lock_fault_reports_the_locked_clock_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault after the lock reports the decision that attempt used.

    Another firing can record a newer baseline while this one waits for the
    lock, so the pre-lock decision may differ from the locked one. Reporting
    the former would name a clock state the run never acted on.
    """
    store_layout = layout(tmp_path)
    skewed = outcome_archive_clock_guard.ClockReading(
        boot_id="boot",
        wall=NOW - dt.timedelta(days=3),
        boottime=100.0,
    )
    readings = iter([BaselineRead(None, "missing"), BaselineRead(skewed, "recorded")])

    def read_baseline(_layout: object) -> BaselineRead:
        """Return the pre-lock reading, then a baseline another firing wrote."""
        return next(readings)

    def out_of_space(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Fail as a full filesystem would once the lock is held."""
        raise outcome_archive_lock.NoHeadroomError(28, "no space left")

    monkeypatch.setattr(outcome_archive_clock_state, "read_baseline", read_baseline)
    monkeypatch.setattr(outcome_archive_execute, "execute_locked", out_of_space)
    options = RunOptions(now=NOW, boot_id=lambda: "boot", boottime=lambda: 100.0)

    report = run_retain(store_layout, policy(1_000_000), options)

    assert report.status == "no_headroom", "the fault keeps its closed status"
    assert report.clock_skew is True, (
        "the report carries the locked decision, not the pre-lock one"
    )


def test_a_busy_lock_reports_the_pre_lock_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that never took the lock reports the only decision it made."""
    store_layout = layout(tmp_path)
    reading = outcome_archive_clock_guard.ClockReading(
        boot_id="boot", wall=NOW, boottime=100.0
    )
    monkeypatch.setattr(
        outcome_archive_clock_state,
        "read_baseline",
        lambda _layout: BaselineRead(reading, "recorded"),
    )

    def busy(_lock: object) -> typ.NoReturn:
        """Fail as a second firing that cannot take the lock would."""
        raise outcome_archive_lock.ArchiveBusyError

    monkeypatch.setattr(outcome_archive_lock.ArchiveLock, "__enter__", busy)

    report = outcome_archive.run_archive(
        store_layout,
        policy(1_000_000),
        options=RunOptions(now=NOW, boot_id=lambda: "boot", boottime=lambda: 100.0),
    )

    assert report.status == "busy", "the lock is still held by another firing"
    assert report.clock_skew is False, "no locked decision exists to report"


def test_a_reading_that_never_settles_reports_unsettled_and_records_the_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A moving store is retryable, and the firing still records its reading."""
    from buildlimits import outcome_archive_retain
    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_segment_inventory import Inventory

    store_layout = layout(tmp_path)

    def never_settles(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        """Describe a store that changed across every bounded reading."""
        return Inventory(), ActiveHistory(), False

    monkeypatch.setattr(outcome_archive_retain, "settled_snapshot", never_settles)

    report = run_retain(store_layout, policy(1_000_000), RunOptions(now=NOW))

    assert report.status == "unsettled", "the timer retries the firing"
    assert report.plan is None, "no plan is reported from a torn pair"
    assert (
        outcome_archive_clock_state.read_baseline(store_layout).reading is not None
    ), "the firing records its reading even when it plans nothing"
