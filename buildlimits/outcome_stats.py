"""Parse sccache 0.14 statistics into immutable domain values."""

import dataclasses as dc
import json
import typing as typ

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


@dc.dataclass(frozen=True, slots=True)
class StatsReading:
    """Validated scalar counters from one supported sccache reading.

    The three cache-result fields are sums of their ``counts`` maps;
    ``adv_counts`` has a separate meaning and is deliberately excluded.
    """

    compile_requests: int
    requests_unsupported_compiler: int
    requests_not_compile: int
    requests_not_cacheable: int
    requests_executed: int
    cache_hits: int
    cache_misses: int
    cache_errors: int
    non_cacheable_compilations: int
    compilations: int
    compile_fails: int
    version: str


@dc.dataclass(frozen=True, slots=True)
class ReadFailure:
    """A statistics document that could not be admitted as a reading."""

    reason: typ.Literal["parse_error", "unsupported_version"]


def _mapping(value: object) -> cabc.Mapping[str, object] | None:
    """Return a string-keyed JSON object, if ``value`` has that shape.

    Returns
    -------
    Mapping[str, object] or None
        The JSON object's fields, or ``None`` when ``value`` is not an object.

    Examples
    --------
    >>> _mapping({"stats": {}})["stats"]
    {}
    >>> _mapping([]) is None
    True
    """
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return typ.cast("cabc.Mapping[str, object]", value)


def _integer(value: object) -> int:
    """Accept a non-negative JSON integer and reject booleans.

    Returns
    -------
    int
        The validated non-negative counter.

    Raises
    ------
    TypeError
        If ``value`` is not an integer or is a boolean.
    ValueError
        If ``value`` is negative.

    Examples
    --------
    >>> _integer(3)
    3
    >>> _integer(True)
    Traceback (most recent call last):
    ...
    TypeError: counter is not an integer
    """
    if isinstance(value, bool) or not isinstance(value, int):
        message = "counter is not an integer"
        raise TypeError(message)
    if value < 0:
        message = "counter is negative"
        raise ValueError(message)
    return value


def _cache_count(stats: cabc.Mapping[str, object], field: str) -> int:
    """Sum one cache-result ``counts`` map without reading ``adv_counts``.

    Returns
    -------
    int
        The sum of the per-language ``counts`` values.

    Raises
    ------
    TypeError
        If the cache result or its ``counts`` field is not an object.

    Examples
    --------
    >>> _cache_count({"cache_hits": {"counts": {"Rust": 2}}}, "cache_hits")
    2
    """
    cache_result = _mapping(stats.get(field))
    if cache_result is None:
        message = f"statistics field {field!r} is not an object"
        raise TypeError(message)
    counts = _mapping(cache_result.get("counts"))
    if counts is None:
        message = f"statistics field {field!r}.counts is not an object"
        raise TypeError(message)
    return sum(_integer(value) for value in counts.values())


def parse_statistics(
    text: str, *, supported: frozenset[str]
) -> StatsReading | ReadFailure:
    """Parse a supported sccache JSON document without leaking raw data.

    Returns
    -------
    StatsReading or ReadFailure
        Validated counters, or a closed reason for a document that cannot be read.

    Examples
    --------
    >>> parse_statistics(
    ...     '{"version":"0.15.0","stats":{}}',
    ...     supported=frozenset({"0.14.0"}),
    ... )
    ReadFailure(reason='unsupported_version')
    """
    try:
        document_value = json.loads(text)
    except ValueError, TypeError, RecursionError:
        document_value = None

    document = _mapping(document_value)
    if document is None:
        return ReadFailure(reason="parse_error")
    version = document.get("version")
    stats = _mapping(document.get("stats"))
    if not isinstance(version, str) or not version:
        return ReadFailure(reason="parse_error")
    if stats is None:
        return ReadFailure(reason="parse_error")
    if version not in supported:
        return ReadFailure(reason="unsupported_version")

    try:
        scalar_values = {field: _integer(stats[field]) for field in _SCALAR_COUNTERS}
        cache_values = {field: _cache_count(stats, field) for field in _CACHE_COUNTERS}
    except KeyError, TypeError, ValueError:
        return ReadFailure(reason="parse_error")

    return StatsReading(**scalar_values, **cache_values, version=version)
