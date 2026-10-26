"""Schema and report contracts for outcome-recording telemetry."""

import datetime as dt
import json
import math
import typing as typ

from journal_fixtures import journal_runner
from outcome_vocabulary_test_support import OUTCOME_ALIASES

from buildlimits import metrics_schema
from buildlimits.metrics_query import query
from buildlimits.metrics_report import report
from buildlimits.outcome_record_format import ChildSighting, Origin


def test_outcome_schema_vocabularies_match_domain_literals() -> None:
    """Journal vocabulary is the same closed set used by outcome ports."""
    expected = {
        **OUTCOME_ALIASES,
        "crate_origin": Origin,
        "child_sighting": ChildSighting,
    }
    for name, alias in expected.items():
        assert metrics_schema.VOCABULARIES[name] == frozenset(
            typ.get_args(alias.__value__)
        ), f"{name} matches its Literal alias"


def test_outcome_event_fields_are_registered_and_bounded() -> None:
    """Each new event and field kind is part of the fixed journal schema."""
    assert {
        "cache_outcome",
        "outcome_reading",
        "outcome_store",
    } <= metrics_schema.EVENTS, "all outcome event grains are accepted"
    assert {
        "cache_result",
        "outcome_attribution",
        "crate_origin",
        "outcome_reason",
        "store_operation",
        "store_reason",
        "evaluation_reason",
        "gate_reason",
        "child_sighting",
    } <= metrics_schema.FIELD_KINDS.keys(), "outcome labels are schema-owned"
    assert {
        "window_groups",
        "readings",
        "readings_skipped",
        "readings_failed",
        "store_records",
        "store_discarded",
        "child_samples",
        "child_samples_taken",
        "child_sample_failures",
    } <= metrics_schema.COUNTERS, "outcome counts are bounded counters"


def test_empty_outcome_report_section_is_empty() -> None:
    """Older journals produce an empty optional outcome section."""
    window = query(runner=journal_runner(""))

    assert report(window)["cache_outcomes"] == {}, "no synthetic zero counts"


def test_outcome_report_counts_attribution_and_reasons() -> None:
    """Coverage follows attribution, independently of a result token."""
    common = {
        "BUILD_LIMITS_SCHEMA": "1",
        "BUILD_LIMITS_SOURCE": "supervisor",
        "_SYSTEMD_USER_UNIT": "buildlimits-supervisor.service",
        "__REALTIME_TIMESTAMP": str(int(dt.datetime.now(dt.UTC).timestamp() * 1e6)),
    }
    events = [
        {
            **common,
            "BUILD_LIMITS_EVENT": "cache_outcome",
            "BUILD_LIMITS_CACHE_RESULT": "miss",
            "BUILD_LIMITS_OUTCOME_ATTRIBUTION": "solo",
            "BUILD_LIMITS_OUTCOME_REASON": "none",
            "BUILD_LIMITS_CHILD_SIGHTING": "seen",
        },
        {
            **common,
            "BUILD_LIMITS_EVENT": "cache_outcome",
            "BUILD_LIMITS_CACHE_RESULT": "unknown-result",
            "BUILD_LIMITS_OUTCOME_ATTRIBUTION": "none",
            "BUILD_LIMITS_OUTCOME_REASON": "timeout",
            "BUILD_LIMITS_CHILD_SIGHTING": "unavailable",
        },
        {
            **common,
            "BUILD_LIMITS_EVENT": "request_admitted",
            "BUILD_LIMITS_CACHE_PATH": "cached",
        },
        {
            **common,
            "BUILD_LIMITS_EVENT": "outcome_reading",
            "BUILD_LIMITS_CHILD_SAMPLES_TAKEN": "2",
            "BUILD_LIMITS_CHILD_SAMPLE_FAILURES": "1",
        },
        {
            **common,
            "BUILD_LIMITS_EVENT": "request_admitted",
            "BUILD_LIMITS_CACHE_PATH": "cached",
        },
    ]
    window = query(runner=journal_runner("\n".join(map(json.dumps, events))))

    outcomes = typ.cast("dict[str, typ.Any]", report(window)["cache_outcomes"])
    assert outcomes["outcomes"] == {"miss": 1}, "unknown result is omitted alone"
    assert outcomes["attributed"] == 1, "attribution drives coverage"
    assert math.isclose(outcomes["coverage"], 0.5), (
        "coverage uses cached launch admissions"
    )
    assert outcomes["reasons"] == {"none": 1, "timeout": 1}, "reasons are counted"
    assert outcomes["child_sightings"] == {"seen": 1, "unavailable": 1}, (
        "the report counts the closed child-sighting vocabulary"
    )
    assert outcomes["readings"]["child_samples_taken"] == 2, (
        'outcome_report_counts_attri...: outcomes["readings"]["child_samples_'
    )
    assert outcomes["readings"]["child_sample_failures"] == 1, (
        'outcome_report_counts_attri...: outcomes["readings"]["child_sample_f'
    )


def test_outcome_tracing_vocabulary_is_registered() -> None:
    """The releasable schema-only plateau recognizes later span operations."""
    from buildlimits.tracing_export import FAILURE_CATEGORIES, OPERATIONS, OUTCOMES

    assert {
        "statistics_read",
        "admission_selection",
        "outcome_store_load",
        "outcome_store_save",
        "outcome_store_compact",
    } <= OPERATIONS, "recording span operations are fixed"
    assert {"statistics_query", "store_io", "salt"} <= FAILURE_CATEGORIES, (
        "recording failure categories are fixed"
    )
    assert "completed" in OUTCOMES, "recording completion is a known span outcome"
