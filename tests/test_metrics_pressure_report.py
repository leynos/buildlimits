"""Pressure reports preserve source trust and distinguish observation scopes."""

import json
import subprocess
import typing as typ
from pathlib import Path

import pytest

from buildlimits.metrics_query import MetricsQueryError, Window, query
from buildlimits.metrics_report import report

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

FIXTURE = Path(__file__).parent / "fixtures" / "metrics_pressure.jsonl"
LEGACY_FIXTURE = Path(__file__).parent / "fixtures" / "metrics.jsonl"


def _window(
    output: str | None = None, source: str = "all", cap: int = 10_000
) -> Window:
    """Read fixture observations through the public capped and trusted query."""
    body = FIXTURE.read_text() if output is None else output
    return query(
        source=source,
        max_records=cap,
        runner=lambda argv: subprocess.CompletedProcess(argv, 0, body, ""),
    )


def _pressure(window: Window) -> dict[str, typ.Any]:
    """Read the optional nested result for focused semantic assertions."""
    return typ.cast("dict[str, typ.Any]", report(window)["pressure"])


def test_pressure_report(snapshot: SnapshotAssertion) -> None:
    """The mixed report excludes an untrusted unit and keeps observed scopes explicit."""
    window = _window()
    result = _pressure(window)
    assert len(window.records) == 16, (
        "the untrusted seventeenth observation is rejected"
    )
    descriptors = result["descriptors"]
    assert descriptors["latest_observed"] == {
        "retained": 0,
        "budget": 0,
        "budget_known": False,
    }, "a fail-closed capacity is zero, with an explicitly unknown baseline"
    assert descriptors["maximum_observed_lifetime_high_water"] == 25, (
        "lifetime snapshots survive restarts without accepting untrusted maxima"
    )
    assert result["queue"]["maximum_observed_lifetime_high_water"] == 12, (
        "queue high-water is an observed lifetime maximum, not a current value"
    )
    assert result == snapshot, "bounded pressure aggregates snapshot"


def test_accept_partial_and_completed_observations() -> None:
    """Paused zeroes and failed-resume elapsed samples cannot dilute completed durations."""
    accept = _pressure(_window())["accept"]
    assert accept["states"] == {"paused": 1, "resume_failed": 2, "resumed": 1}, (
        "each state transition is a window observation"
    )
    assert accept["completed_pause_seconds"]["count"] == 1, (
        "only resumed episodes complete"
    )
    for field in ("p50", "p90", "p99", "maximum"):
        assert accept["completed_pause_seconds"][field] == pytest.approx(12), (
            "completed statistics exclude partial elapsed observations"
        )
    assert accept["resume_failed_elapsed_seconds_maximum"] == pytest.approx(8), (
        "failed attempts expose partial elapsed time separately"
    )
    assert accept["latest_observed_paused"] is False, "the resumed flag remains false"


def test_unfinished_pause_has_no_completed_duration() -> None:
    """A failed resume supplies elapsed progress without inventing completion."""
    output = "\n".join(FIXTURE.read_text().splitlines()[:4])
    accept = _pressure(_window(output))["accept"]
    assert accept["completed_pause_seconds"]["count"] == 0, (
        "no completed episode observed"
    )
    assert accept["completed_pause_seconds"]["maximum"] is None, (
        "full duration remains unknown"
    )
    assert accept["resume_failed_elapsed_seconds_maximum"] == pytest.approx(8), (
        "elapsed partial progress remains visible"
    )
    assert accept["latest_observed_paused"] is True, "the listener remains paused"


def test_cumulative_drain_snapshots_are_not_summed() -> None:
    """Repeated and restarted counter samples remain maxima, distinct from event counts."""
    drain = _pressure(_window())["drain"]
    empty = drain["operations"]["empty_check"]
    assert empty["window_observations"] == 5, "count selected-window observations"
    assert empty["maximum_observed_lifetime_counters"] == {
        "attempts": 12,
        "failures": 3,
        "recoveries": 2,
    }, "cumulative snapshots never sum, even across restarts"
    assert empty["states"]["recovered"] == 1, (
        "window recoveries count events, not lifetime totals"
    )
    cache = drain["operations"]["accounted_cache"]
    assert cache["states"] == {"pending": 1}, "live cached work is pending"
    assert cache["maximum_observed_lifetime_counters"]["failures"] == 0, (
        "a known zero counter remains zero"
    )
    assert drain["operations"]["terminate"]["reasons"] == {"control_error": 1}, (
        "fixed operations retain separate observations"
    )


def test_missing_descriptor_fields_are_not_stitched() -> None:
    """The most recent descriptor snapshot cannot borrow an older capacity or known flag."""
    lines = FIXTURE.read_text().splitlines()
    latest = json.loads(lines[9])
    latest.pop("BUILD_LIMITS_FD_BUDGET")
    latest.pop("BUILD_LIMITS_FD_BUDGET_KNOWN")
    lines[9] = json.dumps(latest)
    current = _pressure(_window("\n".join(lines)))["descriptors"]["latest_observed"]
    assert current == {"retained": 0, "budget": None, "budget_known": None}, (
        "absent capacity and baseline knowledge remain unavailable"
    )


def test_latest_snapshot_uses_timestamp_not_maximum_retention() -> None:
    """Out-of-order journal records select one latest timestamp rather than largest gauges."""
    lines = FIXTURE.read_text().splitlines()
    output = "\n".join([lines[9], lines[0]])
    current = _pressure(_window(output))["descriptors"]["latest_observed"]
    assert current["retained"] == 0, "the newer zero snapshot is current"
    assert current["budget"] == 0 and current["budget_known"] is False, (
        "all descriptor values come from the same timestamped record"
    )


def test_undated_snapshots_use_journal_order() -> None:
    """When no timestamp is available, the last observed snapshot is selected."""
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()[:2]]
    for row in rows:
        row.pop("__REALTIME_TIMESTAMP")
    current = _pressure(_window("\n".join(json.dumps(row) for row in rows)))[
        "descriptors"
    ]["latest_observed"]
    assert current["retained"] == 6, (
        "undated records preserve journal observation order"
    )


def test_adapter_pressure_stays_self_reported() -> None:
    """Busy retries are invocation samples and never become trusted descriptor gauges."""
    pressure = _pressure(_window(source="adapter"))
    assert pressure["adapters"]["self_reported"] is True, (
        "adapter trust remains explicit"
    )
    retries = pressure["adapters"]["busy_retries"]
    assert retries["count"] == 2, (
        "missing retry fields are omitted, not replaced by zero"
    )
    assert retries["p50"] == pytest.approx(0) and retries["p90"] == pytest.approx(2), (
        "explicit zero is one of the two invocation samples"
    )
    assert pressure["descriptors"]["latest_observed"]["retained"] is None, (
        "adapter-only windows cannot invent supervisor measurements"
    )
    assert pressure["drain"]["window_observations"] == 0, (
        "no trusted drain observations"
    )


def test_supervisor_selection_excludes_adapter_retry_samples() -> None:
    """Trusted supervisor selection cannot acquire self-reported invocation measurements."""
    pressure = _pressure(_window(source="supervisor"))
    assert pressure["adapters"]["busy_retries"]["count"] == 0, (
        "adapter samples were filtered"
    )
    assert pressure["adapters"]["busy_retries"]["maximum"] is None, (
        "no invented retry value"
    )
    assert pressure["descriptors"]["latest_observed"]["budget"] == 0, (
        "trusted descriptor measurements remain present"
    )


def test_invalid_pressure_fields_are_omitted() -> None:
    """A valid event survives invalid pressure values without retaining private categories."""
    raw = json.loads(FIXTURE.read_text().splitlines()[6])
    raw.update(
        BUILD_LIMITS_DRAIN_REASON="/private/group",
        BUILD_LIMITS_DRAIN_FAILURES="-1",
        BUILD_LIMITS_DRAIN_RECOVERIES=["1"],
    )
    window = _window(json.dumps(raw))
    operation = _pressure(window)["drain"]["operations"]["empty_check"]
    assert operation["reasons"] == {}, "unknown reason text is not a category"
    counters = operation["maximum_observed_lifetime_counters"]
    assert counters["failures"] is None and counters["recoveries"] is None, (
        "invalid or structured counters remain absent rather than zero"
    )
    assert "private" not in str(report(window)), (
        "invalid labels never reach report history"
    )


def test_invalid_pressure_fields_do_not_enable_optional_section() -> None:
    """Invalid added fields on a legacy event do not create invented pressure history."""
    raw = json.loads(LEGACY_FIXTURE.read_text().splitlines()[0])
    raw.update(BUILD_LIMITS_FD_BUDGET="-1", BUILD_LIMITS_FD_BUDGET_KNOWN="unknown")
    assert "pressure" not in report(_window(json.dumps(raw))), (
        "section presence follows validated measurements, not raw field names"
    )


def test_pressure_raw_cap_precedes_source_filter() -> None:
    """Rejected and other-source records still fill the raw truncation cap."""
    window = _window(source="adapter", cap=17)
    assert window.truncated and len(window.records) == 3, (
        "raw count controls truncation"
    )
    with pytest.raises(MetricsQueryError, match="record cap"):
        _window(source="adapter", cap=16)


@pytest.mark.parametrize("output", ["", LEGACY_FIXTURE.read_text()])
def test_legacy_reports_have_no_pressure_section(output: str) -> None:
    """Legacy and empty windows retain their original report shape."""
    assert "pressure" not in report(_window(output)), (
        "pressure is optional when unobserved"
    )
