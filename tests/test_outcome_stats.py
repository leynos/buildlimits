"""Contract tests for parsing pinned sccache statistics documents."""

import json
import typing as typ
from pathlib import Path

import pytest

_FIXTURE = Path(__file__).parent / "fixtures" / "sccache_0_14_0_stats.json"
_SUPPORTED = frozenset({"0.14.0"})


def test_fixture_is_flattened_into_the_domain_reading() -> None:
    """Parse the checked-in 0.14.0 fixture and ignore advanced counts."""
    from buildlimits.outcome_stats import StatsReading, parse_statistics

    reading = parse_statistics(
        _FIXTURE.read_text(encoding="utf-8"), supported=_SUPPORTED
    )

    assert reading == StatsReading(
        compile_requests=15917,
        requests_unsupported_compiler=0,
        requests_not_compile=168,
        requests_not_cacheable=6271,
        requests_executed=9478,
        cache_hits=7047,
        cache_misses=2387,
        cache_errors=7,
        non_cacheable_compilations=0,
        compilations=2387,
        compile_fails=37,
        version="0.14.0",
    ), "the pinned fixture must flatten into its expected counters"


def test_advanced_counts_are_not_part_of_the_result() -> None:
    """Ignore advanced counters, which have a different dimensionality."""
    from buildlimits.outcome_stats import StatsReading, parse_statistics

    document = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    stats = typ.cast("dict[str, object]", document["stats"])
    for counter in ("cache_hits", "cache_misses", "cache_errors"):
        cache_counter = typ.cast("dict[str, object]", stats[counter])
        cache_counter["adv_counts"] = {"not a counter": "ignored"}

    reading = parse_statistics(json.dumps(document), supported=_SUPPORTED)

    assert isinstance(reading, StatsReading), (
        "advanced counters must not invalidate the reading"
    )
    assert reading.cache_hits == 7047, "only the cache-hit counts map contributes"
    assert reading.cache_misses == 2387, "only the cache-miss counts map contributes"
    assert reading.cache_errors == 7, "only the cache-error counts map contributes"


def test_cache_result_counts_sum_all_languages() -> None:
    """Sum the per-language result counts into one domain counter."""
    from buildlimits.outcome_stats import StatsReading, parse_statistics

    document = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    stats = typ.cast("dict[str, object]", document["stats"])
    cache_hits = typ.cast("dict[str, object]", stats["cache_hits"])
    counts = typ.cast("dict[str, object]", cache_hits["counts"])
    counts["C++"] = 2

    reading = parse_statistics(json.dumps(document), supported=_SUPPORTED)

    assert isinstance(reading, StatsReading), "the fixture remains a supported reading"
    assert reading.cache_hits == 7049, "counts from every language are summed"


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ("", "parse_error"),
        ('{"version":"0.14.0","stats":{}}', "parse_error"),
        ('{"version":"0.14.0","stats":null}', "parse_error"),
        ('{"version":"0.14.0","stats":[]}', "parse_error"),
    ],
    ids=("empty", "partial", "null-stats", "wrong-stats-shape"),
)
def test_invalid_documents_return_a_parse_failure(
    document: str, reason: typ.Literal["parse_error"]
) -> None:
    """Return a typed failure for empty, partial and malformed documents."""
    from buildlimits.outcome_stats import ReadFailure, parse_statistics

    reading = parse_statistics(document, supported=_SUPPORTED)

    assert reading == ReadFailure(reason=reason), (
        "invalid JSON shapes return a parse failure"
    )


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        pytest.param(
            ("stats", "compile_requests"),
            True,
            "parse_error",
            id="boolean-counter",
        ),
        pytest.param(
            ("version",),
            "0.15.0",
            "unsupported_version",
            id="unsupported-version",
        ),
    ],
)
def test_fixture_admission_failures_are_distinct(
    path: tuple[str, ...],
    value: object,
    reason: typ.Literal["parse_error", "unsupported_version"],
) -> None:
    """Reject non-integer counters and unsupported versions distinctly."""
    from buildlimits.outcome_stats import ReadFailure, parse_statistics

    document = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    field_container = typ.cast("dict[str, object]", document)
    for field in path[:-1]:
        field_container = typ.cast("dict[str, object]", field_container[field])
    field_container[path[-1]] = value

    reading = parse_statistics(json.dumps(document), supported=_SUPPORTED)

    assert reading == ReadFailure(reason=reason), (
        "each invalid fixture edit maps to its specific closed failure"
    )


def test_malformed_stats_precede_unsupported_version() -> None:
    """Treat a missing stats object as malformed before checking its version."""
    from buildlimits.outcome_stats import ReadFailure, parse_statistics

    document = {"version": "0.15.0"}

    assert parse_statistics(json.dumps(document), supported=_SUPPORTED) == ReadFailure(
        reason="parse_error"
    ), "a missing stats object is malformed before version admission"


def test_literal_aliases_match_the_metrics_schema_tables() -> None:
    """Keep the domain types and their canonical vocabulary tables aligned."""
    from buildlimits import metrics_schema
    from buildlimits import outcome_vocabulary as vocabulary

    aliases_and_tables = (
        (vocabulary.CacheResult, metrics_schema.CACHE_RESULTS),
        (vocabulary.OutcomeReason, metrics_schema.OUTCOME_REASONS),
        (vocabulary.StoreReason, metrics_schema.STORE_REASONS),
        (vocabulary.EvaluationReason, metrics_schema.EVALUATION_REASONS),
        (vocabulary.StoreOperation, metrics_schema.STORE_OPERATIONS),
        (vocabulary.GateReason, metrics_schema.GATE_REASONS),
    )
    for alias, table in aliases_and_tables:
        assert frozenset(typ.get_args(alias.__value__)) == table, (
            f"{alias.__name__} must match its metrics schema vocabulary"
        )
