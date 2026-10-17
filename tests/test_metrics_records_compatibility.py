"""Public query imports remain compatible with the pure record boundary."""

import json
import subprocess

import pytest

from buildlimits import metrics_query, metrics_records


def test_query_record_constructor_compatibility() -> None:
    """The existing Record import constructs the same values returned by query."""
    assert metrics_query.Record is metrics_records.Record, (
        "the original import is an alias"
    )
    assert metrics_query.SUPERVISOR_UNIT == metrics_records.SUPERVISOR_UNIT, (
        "the existing supervisor-unit constant remains available"
    )
    raw = {
        "BUILD_LIMITS_SCHEMA": "1",
        "BUILD_LIMITS_SOURCE": "supervisor",
        "BUILD_LIMITS_EVENT": "request_finished",
        "BUILD_LIMITS_OUTCOME": "completed",
        "_SYSTEMD_USER_UNIT": metrics_query.SUPERVISOR_UNIT,
    }
    window = metrics_query.query(
        runner=lambda argv: subprocess.CompletedProcess(argv, 0, json.dumps(raw), "")
    )
    positional = metrics_query.Record(
        "supervisor", "request_finished", {"outcome": "completed"}, None
    )
    keyword = metrics_query.Record(
        source="supervisor",
        event="request_finished",
        fields={"outcome": "completed"},
        timestamp=None,
    )
    assert window.records == (positional,), (
        "public query retains the record constructor contract"
    )
    assert positional == keyword, (
        "positional and keyword construction remain compatible"
    )


def test_query_error_import_compatibility() -> None:
    """Malformed journal JSON raises the same catchable public exception alias."""
    assert metrics_query.MetricsQueryError is metrics_records.MetricsQueryError, (
        "the original error import remains the canonical exception class"
    )
    with pytest.raises(
        metrics_query.MetricsQueryError, match="malformed JSON"
    ) as caught:
        metrics_query.query(
            runner=lambda argv: subprocess.CompletedProcess(argv, 0, "not-json", "")
        )
    assert isinstance(caught.value, metrics_records.MetricsQueryError), (
        "both import paths catch the same decoding error"
    )
    assert isinstance(caught.value.__cause__, json.JSONDecodeError), (
        "the original malformed-JSON cause remains chained"
    )
