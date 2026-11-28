"""The clock decision and baseline one firing records.

The baseline withholds age eviction for one firing, so its own failure must be
reported without discarding the state flags of the report it failed beside. A
report that claims a baseline was written when it was not would send an
operator looking for a file that does not exist.
"""

import json
import typing as typ
from pathlib import Path

from outcome_archive_helpers import NOW, layout, policy, refuse

from buildlimits import outcome_archive_clock_guard, outcome_archive_firing
from buildlimits.outcome_archive import RunOptions, run_retain
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
    monkeypatch.setattr(outcome_archive_clock_guard, "write_baseline", refuse)
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
