"""Outcome-only telemetry for queries that bypass compile admission."""

import json
import typing as typ
from unittest import mock

import pytest
from conftest import RecordingSink
from journal_fixtures import journal_runner

from buildlimits import adapter_metrics, metrics
from buildlimits.adapter_metrics import AdapterMetrics
from buildlimits.metrics import Metrics
from buildlimits.metrics_query import query
from buildlimits.metrics_report import report


def test_exempted_emits_only_one_outcome_record() -> None:
    """Exemption is terminal, outcome-only, and idempotent with ``finish``."""
    sink = RecordingSink()
    summary = AdapterMetrics(lambda: 10.0, 2.0, Metrics(sink, source="adapter"))

    summary.exempted()
    summary.finish()
    summary.exempted()

    assert len(sink.records) == 1, "terminal calls emit one summary"
    record = sink.records[0]
    assert record["BUILD_LIMITS_SCHEMA"] == "1", "schema remains current"
    assert record["BUILD_LIMITS_SOURCE"] == "adapter", "source remains bounded"
    assert record["BUILD_LIMITS_EVENT"] == "adapter_finished", "event is terminal"
    assert record["BUILD_LIMITS_OUTCOME"] == "exempt", "outcome records the bypass"
    for field in (
        "BUILD_LIMITS_CONNECTION_ATTEMPTS",
        "BUILD_LIMITS_BUSY_RETRIES",
        "BUILD_LIMITS_RESTART_RETRIES",
        "BUILD_LIMITS_WAIT_SECONDS",
        "BUILD_LIMITS_RUNTIME_SECONDS",
    ):
        assert field not in record, "exemption adds no admission distribution value"


def test_exempted_suppresses_emission_failure_and_closes_owned_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An owned journal closes in the exempt path even when send fails."""
    sink = mock.Mock()
    sink.emit.side_effect = OSError("journal unavailable")
    monkeypatch.setattr(adapter_metrics, "JournalSink", lambda **_kwargs: sink)
    summary = AdapterMetrics(lambda: 1.0, 0.0)

    summary.exempted()
    summary.finish()

    sink.close.assert_called_once_with()


@pytest.mark.parametrize("older_reader", [False, True], ids=["current", "older"])
def test_exempt_report_has_no_distribution_samples(
    monkeypatch: pytest.MonkeyPatch, older_reader: bool
) -> None:
    """Exempt outcomes never add samples, even if an older reader drops them."""
    sink = RecordingSink()
    summary = AdapterMetrics(lambda: 1.0, 0.0, Metrics(sink, source="adapter"))
    summary.exempted()
    if older_reader:
        monkeypatch.setitem(
            metrics.VOCABULARIES,
            "outcome",
            metrics.OUTCOMES - {"exempt"},
        )

    window = query(runner=journal_runner(json.dumps(sink.records[0])))
    result = typ.cast("dict[str, typ.Any]", report(window))
    expected_outcomes = {} if older_reader else {"exempt": 1}
    assert result["adapters"]["outcomes"] == expected_outcomes, (
        "the current reader keeps exempt and an older reader drops its outcome"
    )
    for distribution in (
        "connection_attempts",
        "wait_seconds",
        "runtime_seconds",
    ):
        assert result["adapters"][distribution]["count"] == 0, (
            "outcome-only records add no distribution samples"
        )
