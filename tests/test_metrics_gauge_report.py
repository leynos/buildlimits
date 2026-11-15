"""Periodic gauge snapshots through the trusted journal query and report.

These cases pin the ``gauges`` event and the report's time-weighted queue
aggregation: a legacy window, a new-format window, a mixed upgrade window, the
schema registration, invalid measurements and source trust.
"""

import json
import typing as typ
from pathlib import Path

import pytest
from journal_fixtures import journal_runner

from buildlimits.metrics import (
    COUNTERS,
    DURATIONS,
    EVENTS,
    SUPERVISOR_WIDE_FIELDS,
)
from buildlimits.metrics_query import query
from buildlimits.metrics_report import report

LEGACY_FIXTURE = Path(__file__).parent / "fixtures" / "metrics.jsonl"
GAUGES_FIXTURE = Path(__file__).parent / "fixtures" / "metrics_gauges.jsonl"
MIXED_FIXTURE = Path(__file__).parent / "fixtures" / "metrics_mixed.jsonl"

# One row per window shape: accepted records, queue maximum, held maximum,
# time-weighted mean, coverage seconds and the deprecated sampled mean.
type Aggregates = tuple[int, float, float, float | None, float | None, float]


def _gauges(fixture: Path) -> tuple[int, dict[str, typ.Any]]:
    """Read a fixture and return its accepted count with its gauge section.

    Returns
    -------
    tuple[int, dict[str, Any]]
        The number of accepted records and the report's ``gauges`` section.
    """
    window = query(runner=journal_runner(fixture.read_text()))
    result = typ.cast("dict[str, typ.Any]", report(window))
    return len(window.records), typ.cast("dict[str, typ.Any]", result["gauges"])


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        pytest.param(
            LEGACY_FIXTURE,
            (10, 2.0, 2.0, None, None, 1.0),
            id="legacy-window",
        ),
        pytest.param(
            GAUGES_FIXTURE,
            (12, 2.0, 2.0, 0.3, 30.0, 0.0),
            id="periodic-snapshots",
        ),
        pytest.param(
            MIXED_FIXTURE,
            (5, 7.0, 8.0, 2.0, 30.0, 1.5),
            id="mixed-upgrade-window",
        ),
    ],
)
def test_report_publishes_the_aggregates_of_each_window_shape(
    fixture: Path, expected: Aggregates
) -> None:
    """Maxima combine both formats; only snapshot intervals weight the mean."""
    count, gauges = _gauges(fixture)
    accepted, queue_max, held_max, weighted, coverage, sample_mean = expected
    assert count == accepted, "untrusted records stay rejected"
    assert gauges["queue_maximum"] == pytest.approx(queue_max), (
        "legacy samples and snapshot peaks share one queue maximum"
    )
    assert gauges["held_maximum"] == pytest.approx(held_max), (
        "legacy samples and snapshot peaks share one held maximum"
    )
    assert gauges["queue_sample_mean"] == pytest.approx(sample_mean), (
        "the deprecated mean reads only non-snapshot records"
    )
    if weighted is None:
        assert gauges["queue_time_weighted_mean"] is None, (
            "no record carries a producer-side interval"
        )
        assert gauges["gauge_coverage_seconds"] is None, "no intervals to cover"
    else:
        assert gauges["queue_time_weighted_mean"] == pytest.approx(weighted), (
            "only snapshots with an interval weight the queue mean"
        )
        assert gauges["gauge_coverage_seconds"] == pytest.approx(coverage), (
            "coverage sums every present interval"
        )


def test_legacy_window_keeps_its_gauge_section_shape() -> None:
    """An upgraded reader adds keys without renaming the legacy contract."""
    _count, gauges = _gauges(LEGACY_FIXTURE)
    assert sorted(gauges) == [
        "gauge_coverage_seconds",
        "held_maximum",
        "queue_maximum",
        "queue_sample_mean",
        "queue_time_weighted_mean",
    ], "the legacy report keeps its gauge section shape"


def test_gauge_schema_registers_the_periodic_event() -> None:
    """The catalogue fixes the event, its field kinds and the wide-field split."""
    assert "gauges" in EVENTS, "periodic snapshots are accepted"
    assert {"queued_peak", "held_peak"} <= COUNTERS, "peaks are counts"
    assert {"interval_seconds", "queued_seconds"} <= DURATIONS, (
        "integrals are durations"
    )
    assert (
        frozenset({
            "queued",
            "held",
            "running",
            "draining",
            "server_drain",
            "capacity",
            "fd_retained",
            "fd_budget",
            "fd_high_water",
            "fd_budget_known",
            "accept_paused",
            "queued_high_water",
        })
        == SUPERVISOR_WIDE_FIELDS
    ), "the supervisor-wide split is explicit"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("queued_peak", "-1", id="negative-count"),
        pytest.param("queued_peak", "1.5", id="fractional-count"),
        pytest.param("held_peak", "many", id="text-count"),
        pytest.param("interval_seconds", "-0.5", id="negative-duration"),
        pytest.param("interval_seconds", "nan", id="non-finite-duration"),
        pytest.param("queued_seconds", "inf", id="infinite-duration"),
    ],
)
def test_invalid_gauge_fields_are_omitted(field: str, value: str) -> None:
    """A snapshot survives an invalid measurement without inventing a value."""
    raw = json.loads(GAUGES_FIXTURE.read_text().splitlines()[5])
    raw[f"BUILD_LIMITS_{field.upper()}"] = value
    window = query(runner=journal_runner(json.dumps(raw)))
    assert len(window.records) == 1, "the event itself remains valid"
    assert field not in window.records[0].fields, "only the invalid field is dropped"


@pytest.mark.parametrize(
    ("source", "unit"),
    [
        pytest.param("adapter", "buildlimits-supervisor.service", id="adapter-source"),
        pytest.param("supervisor", None, id="missing-unit"),
        pytest.param("supervisor", "other.service", id="other-unit"),
    ],
)
def test_gauges_require_a_trusted_supervisor(source: str, unit: str | None) -> None:
    """Periodic snapshots cannot arrive from an adapter or an untrusted unit."""
    raw = json.loads(GAUGES_FIXTURE.read_text().splitlines()[5])
    raw["BUILD_LIMITS_SOURCE"] = source
    if unit is None:
        raw.pop("_SYSTEMD_USER_UNIT")
    else:
        raw["_SYSTEMD_USER_UNIT"] = unit
    assert not query(runner=journal_runner(json.dumps(raw))).records, (
        "the snapshot needs the supervisor's exact unit"
    )
