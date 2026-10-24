"""Normalize pinned sccache statistics for the EP-M1 acceptance fixture."""

import json
import typing as typ
from pathlib import Path

import pytest

# Keep the acceptance fixture's field inventory aligned while preserving adv_counts.
from buildlimits.outcome_stats import (
    _CACHE_COUNTERS,
    _SCALAR_COUNTERS,
    ReadFailure,
    StatsReading,
    parse_statistics,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_WINDOWS_FIXTURE = Path(__file__).parent / "fixtures" / "sccache_0_14_0_windows.jsonl"


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

    Raises
    ------
    TypeError
        If statistics fail validation or the parser returns an unexpected type.
    """
    reading = parse_statistics(json.dumps(document), supported=frozenset({"0.14.0"}))
    match reading:
        case ReadFailure(reason=reason):
            message = f"sccache statistics could not be parsed: {reason}"
            raise TypeError(message)
        case StatsReading() as validated_reading:
            return {
                field: getattr(validated_reading, field)
                for field in (*_SCALAR_COUNTERS, *_CACHE_COUNTERS)
            }
        case _:
            message = "sccache statistics parser returned an unexpected value"
            raise TypeError(message)


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


def _windows_payload(windows: cabc.Sequence[cabc.Mapping[str, object]]) -> str:
    r"""Serialize replay windows using the fixture's stable JSONL format.

    Example
    -------
    >>> _windows_payload([{"name": "cold", "truth": "miss"}])
    '{"name":"cold","truth":"miss"}\n'

    Returns
    -------
    str
        Sorted, compact JSON objects with one trailing newline per window.
    """
    return "".join(
        json.dumps(window, sort_keys=True, separators=(",", ":")) + "\n"
        for window in windows
    )


def _assert_windows_match(windows: cabc.Sequence[cabc.Mapping[str, object]]) -> None:
    """Require observed windows to match the reviewed fixture exactly.

    Example
    -------
    Pass the observations from the real-binary check to validate the committed
    replay data::

        _assert_windows_match(windows)

    A changed observation raises an assertion with the explicit regeneration
    command.

    Raises
    ------
    AssertionError
        If the observed windows differ from the reviewed fixture.
    """
    expected = _WINDOWS_FIXTURE.read_text(encoding="utf-8")
    if _windows_payload(windows) != expected:
        message = (
            f"sccache readings differ from {_WINDOWS_FIXTURE}; set "
            "BUILD_LIMITS_SCCACHE_REGENERATE_FIXTURE=1 to regenerate, then "
            "review the diff"
        )
        raise AssertionError(message)


def _write_windows(windows: cabc.Sequence[cabc.Mapping[str, object]]) -> None:
    """Atomically persist reviewed readings for EP-M2 fixture replay.

    Example
    -------
    After collecting new readings, call ``_write_windows(windows)`` to replace
    the fixture atomically, then inspect the diff before committing it.
    """
    temporary = _WINDOWS_FIXTURE.with_suffix(".jsonl.tmp")
    payload = _windows_payload(windows)
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(_WINDOWS_FIXTURE)
