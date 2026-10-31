"""Counter-footprint tests for conservative cached outcome attribution."""

import itertools as it
import json
import typing as typ
from pathlib import Path

import pytest

if typ.TYPE_CHECKING:
    from buildlimits.outcome_vocabulary import CacheResult

from buildlimits.outcome_attribution import (
    FOOTPRINTS,
    Generation,
    Reading,
    WindowEntry,
    attribute,
)
from buildlimits.outcome_stats import StatsReading, parse_statistics

_COUNTERS = (
    "requests_unsupported_compiler",
    "requests_not_compile",
    "requests_not_cacheable",
    "requests_executed",
    "cache_hits",
    "cache_misses",
    "cache_errors",
    "non_cacheable_compilations",
    "compilations",
    "compile_fails",
)
_GENERATION = Generation(invocation="invocation-a", pid=12, starttime=34)
_WINDOWS_FIXTURE = Path(__file__).parent / "fixtures" / "sccache_0_14_0_windows.jsonl"


def _stats(**changes: int) -> StatsReading:
    """Build one statistics value, defaulting every counter to zero.

    Examples
    --------
    >>> _stats(cache_hits=2).cache_hits
    2
    """
    values = dict.fromkeys(_COUNTERS, 0)
    values["compile_requests"] = 0
    values.update(changes)
    return StatsReading(**values, version="0.14.0")


def _reading(generation: Generation | None = _GENERATION, **changes: int) -> Reading:
    """Build a reading with the requested generation and changed counters.

    Examples
    --------
    >>> _reading(cache_hits=1).stats.cache_hits
    1
    """
    return Reading(generation=generation, stats=_stats(**changes))


def _window(count: int, *, is_clean: bool = True) -> tuple[WindowEntry, ...]:
    """Build a deterministic window of distinct group identifiers.

    Examples
    --------
    >>> [entry.group_id for entry in _window(2)]
    ['g0', 'g1']
    """
    return tuple(
        WindowEntry(group_id=f"g{index}", is_clean=is_clean) for index in range(count)
    )


def _sum_rows(rows: tuple[tuple[str, dict[str, int]], ...]) -> Reading:
    """Create a cumulative reading from named footprint rows.

    Examples
    --------
    >>> _sum_rows((("hit", {"cache_hits": 1, "requests_executed": 1}),)).stats.cache_hits
    1
    """
    totals = dict.fromkeys(_COUNTERS, 0)
    for _, footprint in rows:
        for name, count in footprint.items():
            totals[name] += count
    totals["compile_requests"] = len(rows)
    return Reading(generation=_GENERATION, stats=_stats(**totals))


_FOOTPRINTS = (
    ("hit", {"cache_hits": 1, "requests_executed": 1}),
    ("miss", {"compilations": 1, "cache_misses": 1, "requests_executed": 1}),
    (
        "miss_read_error",
        {
            "compilations": 1,
            "cache_misses": 1,
            "cache_errors": 1,
            "requests_executed": 1,
        },
    ),
    ("not_cached", {"compilations": 1, "requests_executed": 1}),
    (
        "not_cacheable",
        {
            "compilations": 1,
            "non_cacheable_compilations": 1,
            "requests_executed": 1,
        },
    ),
    ("failed", {"compilations": 1, "compile_fails": 1, "requests_executed": 1}),
    ("error_fails", {"compile_fails": 1, "requests_executed": 1}),
    ("error_cache", {"cache_errors": 1, "requests_executed": 1}),
    ("fatal_error", {"cache_errors": 1, "requests_executed": 1}),
    ("silent", {"requests_executed": 1}),
    ("declined", {"requests_not_cacheable": 1}),
    ("not_compile", {"requests_not_compile": 1}),
    ("unsupported", {"requests_unsupported_compiler": 1}),
)
_FOOTPRINT_BY_NAME = dict(_FOOTPRINTS)
_LABELS = {
    "hit": "hit",
    "miss": "miss",
    "miss_read_error": "miss",
    "not_cached": "not_cached",
    "not_cacheable": "not_cacheable",
    "failed": "failed",
    "error_fails": "error",
    "error_cache": "error",
    "fatal_error": "error",
    "silent": None,
    "declined": "declined",
    "not_compile": "not_compile",
    "unsupported": "unsupported",
}
_UNANIMOUS = {"hit", "miss", "not_cached", "not_cacheable", "declined"}


@pytest.mark.parametrize(
    ("name", "footprint", "label"),
    [(name, footprint, _LABELS[name]) for name, footprint in _FOOTPRINTS],
    ids=[name for name, _ in _FOOTPRINTS],
)
def test_truth_table(name: str, footprint: dict[str, int], label: str | None) -> None:
    """Attribute each identifiable solo footprint and reject silent rows."""
    after = _sum_rows(((name, footprint),))

    result = attribute(_reading(), after, _window(1))

    if label is None:
        assert result.labels == (("g0", "unattributed"),), (
            "silent solo rows stay unattributed"
        )
        assert result.kind == "none", "a silent row has no attribution"
        assert result.reason == "count_mismatch", (
            "a silent row does not identify an outcome"
        )
    else:
        assert result.labels == (("g0", label),), (
            "the identifiable solo row keeps its label"
        )
        assert result.kind == "solo", "one identifiable launch is a solo attribution"
        assert result.reason == "none", "an identifiable solo row has no failure reason"


def test_counter_table_exposes_thirteen_documented_rows() -> None:
    """Keep the public counter table aligned with all Table A variants."""
    assert len(FOOTPRINTS) == 13, "Table A has thirteen documented counter rows"
    assert sum(footprint.label is None for footprint in FOOTPRINTS) == 1, (
        "only the silent footprint lacks a solo label"
    )


def test_captured_windows_replay_to_their_known_outcomes() -> None:
    """Keep the captured sccache windows aligned with the pure classifier."""
    window_count = 0

    for line in _WINDOWS_FIXTURE.read_text(encoding="utf-8").splitlines():
        document = typ.cast("dict[str, object]", json.loads(line))
        name = typ.cast("str", document["name"])
        truth = typ.cast("CacheResult", document["truth"])
        before = parse_statistics(
            json.dumps(document["before"]), supported=frozenset({"0.14.0"})
        )
        after = parse_statistics(
            json.dumps(document["after"]), supported=frozenset({"0.14.0"})
        )

        assert isinstance(before, StatsReading), (
            f"the {name} baseline is a supported statistics reading"
        )
        assert isinstance(after, StatsReading), (
            f"the {name} result is a supported statistics reading"
        )
        result = attribute(
            Reading(_GENERATION, before),
            Reading(_GENERATION, after),
            (WindowEntry(group_id=name, is_clean=True),),
        )

        assert result.labels == ((name, truth),), (
            f"the {name} counter delta matches its captured outcome"
        )
        assert result.kind == "solo", f"the {name} fixture describes one launch"
        window_count += 1

    assert window_count >= 200, "the replay retains H1's minimum controlled windows"


@pytest.mark.parametrize(
    ("footprints", "label"),
    [
        (("hit", "hit"), "hit"),
        (("miss", "miss_read_error"), "miss"),
        (("not_cached", "not_cached"), "not_cached"),
        (("not_cacheable", "not_cacheable"), "not_cacheable"),
        (("declined", "declined"), "declined"),
    ],
    ids=("hit", "miss-with-read-error", "not-cached", "not-cacheable", "declined"),
)
def test_only_approved_multi_launch_footprints_are_unanimous(
    footprints: tuple[str, ...], label: str
) -> None:
    """Label the complete window when every allowed footprint agrees."""
    rows = tuple((row, _FOOTPRINT_BY_NAME[row]) for row in footprints)

    result = attribute(_reading(), _sum_rows(rows), _window(len(rows)))

    assert result.labels == tuple((f"g{index}", label) for index in range(len(rows))), (
        "every launch in an approved repeated window shares its label"
    )
    assert result.kind == "unanimous", "an approved repeated footprint is unanimous"
    assert result.reason == "none", (
        "an approved repeated footprint has no failure reason"
    )


def test_unanimous_error_window_remains_unattributed() -> None:
    """Do not turn repeated error footprints into a unanimous class."""
    rows = tuple(("error_cache", _FOOTPRINT_BY_NAME["error_cache"]) for _ in range(2))

    result = attribute(_reading(), _sum_rows(rows), _window(2))

    assert result.labels == (("g0", "unattributed"), ("g1", "unattributed")), (
        "cache-error windows remain wholly unattributed"
    )
    assert result.kind == "none", "cache errors are not an approved unanimous class"
    assert result.reason == "mixed_window", "a balanced error window is mixed"


def test_every_mixed_multiset_is_unattributed() -> None:
    """Exhaust every multiset through size four and count balanced mixtures."""
    mixed = 0

    for size in range(1, 5):
        for rows in it.combinations_with_replacement(_FOOTPRINTS, size):
            mixed += _assert_multiset_is_unattributed(size, rows)

    assert mixed >= 500, "the exhaustive table contains a substantial mixed domain"


def _assert_multiset_is_unattributed(
    size: int, rows: tuple[tuple[str, dict[str, int]], ...]
) -> int:
    """Check one multiset's attribution and report whether it was mixed.

    Returns
    -------
    int
        1 when the multiset is a balanced mixture, otherwise 0.
    """
    result = attribute(_reading(), _sum_rows(rows), _window(size))
    row_labels = {_LABELS[name] for name, _ in rows}

    if size == 1:
        expected = "count_mismatch" if None in row_labels else "none"
        assert result.reason == expected, (rows, result)
        return 0

    unanimous_label = next(iter(row_labels)) if len(row_labels) == 1 else None
    if unanimous_label in _UNANIMOUS:
        assert result.kind == "unanimous", (rows, result)
        assert {label for _, label in result.labels} == {unanimous_label}, (
            "every launch in an approved unanimous multiset has the same label",
            rows,
            result,
        )
        return 0

    assert result.kind == "none", (rows, result)
    assert result.reason == "mixed_window", (rows, result)
    assert all(label == "unattributed" for _, label in result.labels), (
        "mixed multisets are not split into per-group labels",
        rows,
        result,
    )
    return 1


def test_rfc_ambiguous_pair_has_one_shared_unattributed_delta() -> None:
    """Keep failed and not-cached mixes together when their counters collide."""
    not_cached_and_error = _sum_rows((
        ("not_cached", _FOOTPRINT_BY_NAME["not_cached"]),
        ("error_fails", _FOOTPRINT_BY_NAME["error_fails"]),
    ))
    failed_and_silent = _sum_rows((
        ("failed", _FOOTPRINT_BY_NAME["failed"]),
        ("silent", _FOOTPRINT_BY_NAME["silent"]),
    ))

    assert not_cached_and_error.stats == failed_and_silent.stats, (
        "the documented ambiguous pair has one shared counter delta"
    )
    result = attribute(_reading(), not_cached_and_error, _window(2))

    assert result.reason == "mixed_window", "the ambiguous pair remains mixed"
    assert result.kind == "none", "the ambiguous pair receives no per-group attribution"


@pytest.mark.parametrize(
    ("before", "after", "window", "reason"),
    [
        (_reading(generation=None), _reading(), _window(1), "generation_unavailable"),
        (
            _reading(),
            _reading(generation=Generation("invocation-b", 13, 35), compile_requests=3),
            _window(1),
            "generation_changed",
        ),
        (_reading(), _reading(), _window(1), "empty_stats"),
        (
            _reading(compile_requests=5, requests_executed=5),
            _reading(compile_requests=4, requests_executed=4),
            _window(1),
            "counter_decreased",
        ),
        (
            _reading(),
            _sum_rows((("miss", _FOOTPRINT_BY_NAME["miss"]),)),
            _window(1, is_clean=False),
            "ineligible_window",
        ),
        (_reading(), _reading(compile_requests=2), _window(1), "count_mismatch"),
    ],
    ids=(
        "generation-unavailable",
        "generation-changed",
        "empty-stats",
        "counter-decreased",
        "ineligible-window",
        "count-mismatch",
    ),
)
def test_window_failures_are_unattributed(
    before: Reading,
    after: Reading,
    window: tuple[WindowEntry, ...],
    reason: str,
) -> None:
    """Return the first applicable failure reason without splitting a window."""
    result = attribute(before, after, window)

    assert result.labels == tuple(
        (entry.group_id, "unattributed") for entry in window
    ), "a failed reading labels every launch as unattributed"
    assert result.kind == "none", "a failed reading has no attribution"
    assert result.reason == reason, "the first applicable reading failure is returned"


def test_empty_window_has_no_labels_or_failure_reason() -> None:
    """An empty window is a no-op even when its readings are unchanged."""
    result = attribute(_reading(), _reading(), ())

    assert not result.labels, "an empty window produces no labels"
    assert result.kind == "none", "an empty window has no attribution"
    assert result.reason == "none", "an empty window is not a failure"
