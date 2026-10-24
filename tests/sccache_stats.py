"""Normalize pinned sccache statistics for the EP-M1 acceptance fixture."""

import json
import typing as typ
from pathlib import Path

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_SCALAR_COUNTERS = (
    "compile_requests",
    "requests_unsupported_compiler",
    "requests_not_compile",
    "requests_not_cacheable",
    "requests_executed",
    "non_cacheable_compilations",
    "compilations",
    "compile_fails",
)
_CACHE_COUNTERS = ("cache_hits", "cache_misses", "cache_errors")


def _integer(value: object, *, field: str) -> int:
    """Require one JSON statistics value to be a non-negative integer.

    Returns
    -------
    int
        The validated counter value.

    Raises
    ------
    TypeError
        If the value is not an integer or is a boolean.
    ValueError
        If the counter value is negative.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        message = f"statistics field {field!r} is not an integer"
        raise TypeError(message)
    if value < 0:
        message = f"statistics field {field!r} is negative"
        raise ValueError(message)
    return value


def _cache_count(document: cabc.Mapping[str, object], field: str) -> int:
    """Sum per-language counts for one cache result counter.

    Returns
    -------
    int
        The total count across all languages.

    Raises
    ------
    TypeError
        If the counter or its ``counts`` member is not an object.
    """
    value = document.get(field)
    if not isinstance(value, dict):
        message = f"statistics field {field!r} is not an object"
        raise TypeError(message)
    counts = value.get("counts")
    if not isinstance(counts, dict):
        message = f"statistics field {field!r}.counts is not an object"
        raise TypeError(message)
    return sum(_integer(count, field=f"{field}.counts") for count in counts.values())


def _stats_payload(document: cabc.Mapping[str, object]) -> cabc.Mapping[str, object]:
    """Return the nested counter object from one sccache JSON document.

    Returns
    -------
    collections.abc.Mapping[str, object]
        The document's nested statistics object.

    Raises
    ------
    TypeError
        If the document has no object-valued ``stats`` member.
    """
    value = document.get("stats")
    if not isinstance(value, dict):
        message = "sccache statistics document has no stats object"
        raise TypeError(message)
    return typ.cast("cabc.Mapping[str, object]", value)


def _footprint(document: cabc.Mapping[str, object]) -> dict[str, int]:
    """Flatten the counters whose changes identify one sccache result.

    Returns
    -------
    dict[str, int]
        The scalar and cache-result counters in the statistics document.
    """
    stats = _stats_payload(document)
    result = {name: _integer(stats.get(name), field=name) for name in _SCALAR_COUNTERS}
    result.update({name: _cache_count(stats, name) for name in _CACHE_COUNTERS})
    return result


def _fixture_reading(document: cabc.Mapping[str, object]) -> dict[str, object]:
    """Keep stable replay counters and exclude paths and timing metadata.

    Returns
    -------
    dict[str, object]
        A JSON-safe fixture reading containing only stable counter fields.

    Raises
    ------
    TypeError
        If a cache-result counter is not represented by an object.
    """
    stats = _stats_payload(document)
    normalized: dict[str, object] = {name: stats[name] for name in _SCALAR_COUNTERS}
    for name in _CACHE_COUNTERS:
        value = stats[name]
        if not isinstance(value, dict):
            message = f"statistics field {name!r} is not an object"
            raise TypeError(message)
        normalized[name] = {
            key: value[key] for key in ("counts", "adv_counts") if key in value
        }
    return {"version": document["version"], "stats": normalized}


def _assert_delta(
    before: cabc.Mapping[str, object],
    after: cabc.Mapping[str, object],
    expected: cabc.Mapping[str, int],
) -> None:
    """Require one request to have exactly its documented counter footprint."""
    before_counts = _footprint(before)
    after_counts = _footprint(after)
    delta = {name: after_counts[name] - count for name, count in before_counts.items()}
    wanted = dict.fromkeys(before_counts, 0)
    wanted.update(expected)
    if delta != wanted:
        pytest.fail(f"Unexpected sccache counter delta: expected {wanted}, got {delta}")


def _record_window(
    windows: list[dict[str, object]],
    name: str,
    truth: str,
    readings: tuple[dict[str, object], dict[str, object]],
) -> None:
    """Append one replayable window without host-specific cache paths."""
    before, after = readings
    windows.append({
        "name": name,
        "truth": truth,
        "before": _fixture_reading(before),
        "after": _fixture_reading(after),
    })


def _write_windows(windows: cabc.Sequence[cabc.Mapping[str, object]]) -> None:
    """Atomically persist controlled readings for EP-M2 fixture replay."""
    fixture = Path(__file__).parent / "fixtures" / "sccache_0_14_0_windows.jsonl"
    temporary = fixture.with_suffix(".jsonl.tmp")
    payload = "".join(
        json.dumps(window, sort_keys=True, separators=(",", ":")) + "\n"
        for window in windows
    )
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(fixture)
