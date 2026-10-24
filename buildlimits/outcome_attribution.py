"""Attribute sccache counter movements only when a whole window agrees."""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.outcome_stats import StatsReading
    from buildlimits.outcome_vocabulary import CacheResult, OutcomeReason

_SIGNATURE_FIELDS = (
    "cache_hits",
    "compilations",
    "cache_misses",
    "non_cacheable_compilations",
    "compile_fails",
    "cache_errors",
    "requests_executed",
    "requests_not_cacheable",
    "requests_not_compile",
    "requests_unsupported_compiler",
)
_COUNTER_FIELDS = ("compile_requests", *_SIGNATURE_FIELDS)
_MIN_UNANIMOUS_LAUNCHES = 2


@dc.dataclass(frozen=True, slots=True)
class Generation:
    """Identity for one running sccache server generation."""

    invocation: str
    pid: int
    starttime: int


@dc.dataclass(frozen=True, slots=True)
class Reading:
    """One statistics value paired with the server that produced it."""

    generation: Generation | None
    stats: StatsReading


@dc.dataclass(frozen=True, slots=True)
class WindowEntry:
    """One cached launch and whether its ending proves the window is clean."""

    group_id: str
    is_clean: bool


@dc.dataclass(frozen=True, slots=True)
class Attribution:
    """Whole-window labels and the reason when no labels can be assigned."""

    labels: tuple[tuple[str, CacheResult], ...]
    kind: typ.Literal["solo", "unanimous", "none"]
    reason: OutcomeReason


@dc.dataclass(frozen=True, slots=True)
class _Footprint:
    """One identifiable row from the approved sccache counter table."""

    label: CacheResult | None
    values: tuple[int, ...]


def _footprint(label: CacheResult | None, **values: int) -> _Footprint:
    """Build one table row in the fixed signature-counter order.

    Returns
    -------
    _Footprint
        A counter tuple in the fixed signature order.

    Examples
    --------
    >>> _footprint("hit", cache_hits=1).values[0]
    1
    """
    return _Footprint(
        label=label,
        values=tuple(values.get(field, 0) for field in _SIGNATURE_FIELDS),
    )


_HIT_FOOTPRINT = _footprint("hit", cache_hits=1, requests_executed=1)
_MISS_FOOTPRINT = _footprint(
    "miss", compilations=1, cache_misses=1, requests_executed=1
)
_MISS_WITH_READ_ERROR_FOOTPRINT = _footprint(
    "miss",
    compilations=1,
    cache_misses=1,
    cache_errors=1,
    requests_executed=1,
)
_NOT_CACHED_FOOTPRINT = _footprint("not_cached", compilations=1, requests_executed=1)
_NOT_CACHEABLE_FOOTPRINT = _footprint(
    "not_cacheable",
    compilations=1,
    non_cacheable_compilations=1,
    requests_executed=1,
)
_FAILED_FOOTPRINT = _footprint(
    "failed", compilations=1, compile_fails=1, requests_executed=1
)
_ERROR_FAILURE_FOOTPRINT = _footprint("error", compile_fails=1, requests_executed=1)
_ERROR_CACHE_FOOTPRINT = _footprint("error", cache_errors=1, requests_executed=1)
_FATAL_ERROR_FOOTPRINT = _footprint("error", cache_errors=1, requests_executed=1)
_SILENT_FOOTPRINT = _footprint(None, requests_executed=1)
_DECLINED_FOOTPRINT = _footprint("declined", requests_not_cacheable=1)
_NOT_COMPILE_FOOTPRINT = _footprint("not_compile", requests_not_compile=1)
_UNSUPPORTED_FOOTPRINT = _footprint("unsupported", requests_unsupported_compiler=1)

FOOTPRINTS = (
    _HIT_FOOTPRINT,
    _MISS_FOOTPRINT,
    _MISS_WITH_READ_ERROR_FOOTPRINT,
    _NOT_CACHED_FOOTPRINT,
    _NOT_CACHEABLE_FOOTPRINT,
    _FAILED_FOOTPRINT,
    _ERROR_FAILURE_FOOTPRINT,
    _ERROR_CACHE_FOOTPRINT,
    _FATAL_ERROR_FOOTPRINT,
    _SILENT_FOOTPRINT,
    _DECLINED_FOOTPRINT,
    _NOT_COMPILE_FOOTPRINT,
    _UNSUPPORTED_FOOTPRINT,
)
_UNANIMOUS_FOOTPRINTS = (
    _HIT_FOOTPRINT,
    _NOT_CACHED_FOOTPRINT,
    _NOT_CACHEABLE_FOOTPRINT,
    _DECLINED_FOOTPRINT,
)


def _counter_values(stats: StatsReading) -> tuple[int, ...]:
    """Read every cumulative counter used by the window comparison.

    Returns
    -------
    tuple[int, ...]
        Cumulative counters in the fixed comparison order.

    Examples
    --------
    >>> from buildlimits.outcome_stats import StatsReading
    >>> _counter_values(StatsReading(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "0.14.0"))[0]
    0
    """
    return tuple(getattr(stats, field) for field in _COUNTER_FIELDS)


def _unattributed(
    window: tuple[WindowEntry, ...], reason: OutcomeReason
) -> Attribution:
    """Return one fallback label per launch without splitting the window.

    Returns
    -------
    Attribution
        One unattributed result for every launch in the window.

    Examples
    --------
    >>> entry = WindowEntry("g", is_clean=True)
    >>> _unattributed((entry,), "mixed_window").labels
    (('g', 'unattributed'),)
    """
    unattributed: CacheResult = "unattributed"
    return Attribution(
        labels=tuple((entry.group_id, unattributed) for entry in window),
        kind="none",
        reason=reason,
    )


def _balanced(delta: tuple[int, ...], launch_count: int) -> bool:
    """Check whether the counters can be composed from Table A rows.

    Returns
    -------
    bool
        Whether the counter changes can be composed from Table A rows.

    Examples
    --------
    >>> _balanced((1, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0), 1)
    True
    """
    counters = dict(zip(_COUNTER_FIELDS, delta, strict=True))
    other_compilations = (
        counters["compilations"]
        - counters["cache_misses"]
        - counters["non_cacheable_compilations"]
    )
    minimum_executed = (
        counters["cache_hits"]
        + counters["compilations"]
        + max(0, counters["compile_fails"] - other_compilations)
        + max(0, counters["cache_errors"] - counters["cache_misses"])
    )
    return (
        counters["compile_requests"] == launch_count
        and min(delta) >= 0
        and other_compilations >= 0
        and counters["requests_executed"] >= minimum_executed
        and launch_count
        == sum(
            counters[field]
            for field in (
                "requests_executed",
                "requests_not_cacheable",
                "requests_not_compile",
                "requests_unsupported_compiler",
            )
        )
    )


def _unanimous_label(delta: tuple[int, ...], launch_count: int) -> CacheResult | None:
    """Return a label only for an approved repeated footprint.

    Returns
    -------
    CacheResult or None
        An approved repeated label, or ``None`` for every other signature.

    Examples
    --------
    >>> _unanimous_label((2, 0, 0, 0, 0, 0, 2, 0, 0, 0), 2)
    'hit'
    """
    if launch_count < _MIN_UNANIMOUS_LAUNCHES:
        return None
    for footprint in _UNANIMOUS_FOOTPRINTS:
        if delta == tuple(value * launch_count for value in footprint.values):
            return footprint.label

    miss = _MISS_FOOTPRINT.values
    is_repeated_miss = delta[:5] == tuple(
        value * launch_count for value in miss[:5]
    ) and delta[6:] == tuple(value * launch_count for value in miss[6:])
    has_valid_read_errors = 0 <= delta[5] <= launch_count
    if is_repeated_miss and has_valid_read_errors:
        return "miss"
    return None


def _failure_reason(
    before: Reading, after: Reading, window: tuple[WindowEntry, ...]
) -> OutcomeReason | None:
    """Return the first reading-level failure before counter classification.

    Returns
    -------
    OutcomeReason or None
        The first reading failure, or ``None`` when the window can be classified.

    Examples
    --------
    >>> generation = Generation("run", 1, 2)
    >>> from buildlimits.outcome_stats import StatsReading
    >>> empty = StatsReading(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "0.14.0")
    >>> entry = WindowEntry("g", is_clean=True)
    >>> before = Reading(generation, empty)
    >>> _failure_reason(before, before, (entry,))
    'empty_stats'
    """
    if before.generation is None or after.generation is None:
        return "generation_unavailable"
    if before.generation != after.generation:
        return "generation_changed"
    if all(value == 0 for value in _counter_values(after.stats)):
        return "empty_stats"
    if any(
        after_value < before_value
        for before_value, after_value in zip(
            _counter_values(before.stats), _counter_values(after.stats), strict=True
        )
    ):
        return "counter_decreased"
    if any(not entry.is_clean for entry in window):
        return "ineligible_window"
    return None


def _classify_delta(
    delta: tuple[int, ...], launch_count: int
) -> tuple[
    CacheResult | None,
    typ.Literal["solo", "unanimous", "none"],
    OutcomeReason,
]:
    """Classify a balanced counter delta as solo, unanimous or mixed.

    Returns
    -------
    tuple[CacheResult or None, str, OutcomeReason]
        The shared label, attribution kind and fallback reason.

    Examples
    --------
    >>> _classify_delta((1, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0), 1)
    ('hit', 'solo', 'none')
    """
    if not _balanced(delta, launch_count):
        return None, "none", "count_mismatch"

    signature = delta[1:]
    if launch_count == 1:
        for footprint in FOOTPRINTS:
            if signature == footprint.values:
                if footprint.label is None:
                    return None, "none", "count_mismatch"
                return footprint.label, "solo", "none"
        return None, "none", "count_mismatch"

    label = _unanimous_label(signature, launch_count)
    if label is not None:
        return label, "unanimous", "none"
    return None, "none", "mixed_window"


def attribute(
    before: Reading,
    after: Reading,
    window: tuple[WindowEntry, ...],
) -> Attribution:
    """Label a solo or approved unanimous window from its counter delta.

    Returns
    -------
    Attribution
        The whole-window labels, attribution kind and any fallback reason.

    Examples
    --------
    >>> generation = Generation("run", 1, 2)
    >>> from buildlimits.outcome_stats import StatsReading
    >>> stats = StatsReading(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "0.14.0")
    >>> after_stats = dc.replace(
    ...     stats, compile_requests=1, cache_hits=1, requests_executed=1
    ... )
    >>> window = (WindowEntry("g", is_clean=True),)
    >>> before = Reading(generation, stats)
    >>> after = Reading(generation, after_stats)
    >>> attribute(before, after, window).labels
    (('g', 'hit'),)
    """
    if not window:
        return Attribution(labels=(), kind="none", reason="none")

    failure = _failure_reason(before, after, window)
    if failure is not None:
        return _unattributed(window, failure)

    before_values = _counter_values(before.stats)
    after_values = _counter_values(after.stats)
    delta = tuple(
        after_value - before_value
        for before_value, after_value in zip(before_values, after_values, strict=True)
    )
    label, kind, reason = _classify_delta(delta, len(window))
    if label is None:
        return _unattributed(window, reason)
    return Attribution(
        labels=tuple((entry.group_id, label) for entry in window),
        kind=kind,
        reason=reason,
    )
