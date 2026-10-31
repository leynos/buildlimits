"""Public query imports remain compatible with the pure record boundary."""

import json
import subprocess
import typing as typ

import pytest

from buildlimits import metrics_query, metrics_records, metrics_report


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


def test_unknown_outcome_token_keeps_other_fields_and_attribution() -> None:
    """An unknown result drops alone; attribution still drives report coverage."""
    raw = {
        "BUILD_LIMITS_SCHEMA": "1",
        "BUILD_LIMITS_SOURCE": "supervisor",
        "BUILD_LIMITS_EVENT": "cache_outcome",
        "BUILD_LIMITS_CACHE_RESULT": "future-result",
        "BUILD_LIMITS_OUTCOME_ATTRIBUTION": "solo",
        "BUILD_LIMITS_OUTCOME_REASON": "none",
        "_SYSTEMD_USER_UNIT": metrics_query.SUPERVISOR_UNIT,
    }
    admitted = {
        "BUILD_LIMITS_SCHEMA": "1",
        "BUILD_LIMITS_SOURCE": "supervisor",
        "BUILD_LIMITS_EVENT": "request_admitted",
        "BUILD_LIMITS_CACHE_PATH": "cached",
        "_SYSTEMD_USER_UNIT": metrics_query.SUPERVISOR_UNIT,
    }
    window = metrics_query.query(
        runner=lambda argv: subprocess.CompletedProcess(
            argv, 0, "\n".join((json.dumps(raw), json.dumps(admitted))), ""
        )
    )

    assert len(window.records) == 2, "an unknown token does not drop its event"
    outcome = window.records[0]
    assert outcome.fields == {
        "outcome_attribution": "solo",
        "outcome_reason": "none",
    }, "the other known fields survive independently"
    outcomes = typ.cast(
        "dict[str, object]", metrics_report.report(window)["cache_outcomes"]
    )
    assert outcomes["outcomes"] == {}, "the unknown cache result is omitted"
    assert outcomes["attributed"] == 1, "attribution alone drives coverage"
