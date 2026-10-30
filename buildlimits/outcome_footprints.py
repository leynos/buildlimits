"""Approved sccache counter footprints and the classification they permit.

One launch can move only the counters listed in the approved table. A window
is labelled only when its counter delta composes from those rows, so this
module keeps the table beside the arithmetic that reads it: a row and the rule
that accepts it stay in one place.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
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


def _repeated(delta: tuple[int, ...], footprint: _Footprint, count: int) -> bool:
    """Check whether a counter delta repeats one footprint exactly.

    Returns
    -------
    bool
        Whether every counter is the footprint value times ``count``.

    Examples
    --------
    >>> _repeated((2, 0, 0, 0, 0, 0, 0, 0, 0, 0), _footprint(None, cache_hits=1), 2)
    True
    """
    return delta == tuple(value * count for value in footprint.values)


def _is_repeated_miss(delta: tuple[int, ...], launch_count: int) -> bool:
    """Check whether a delta is a repeated miss with only read errors varying.

    Read errors accumulate without incrementing the compilation counters, so
    they are checked separately from the fixed miss footprint.

    Returns
    -------
    bool
        Whether the delta is a repeated miss with a plausible read-error count.

    Examples
    --------
    >>> _is_repeated_miss((0, 2, 2, 0, 0, 1, 2, 0, 0, 0), 2)
    True
    """
    miss = _MISS_FOOTPRINT.values
    has_fixed_compilers = delta[:5] == tuple(value * launch_count for value in miss[:5])
    has_fixed_trailing = delta[6:] == tuple(value * launch_count for value in miss[6:])
    has_plausible_read_errors = 0 <= delta[5] <= launch_count
    return has_fixed_compilers and has_fixed_trailing and has_plausible_read_errors


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
        if _repeated(delta, footprint, launch_count):
            return footprint.label
    if _is_repeated_miss(delta, launch_count):
        return "miss"
    return None


def _solo_label(signature: tuple[int, ...]) -> CacheResult | None:
    """Return the Table A label for exactly one observed footprint.

    Returns
    -------
    CacheResult or None
        The matching label, or ``None`` for an unknown or silent signature.

    Examples
    --------
    >>> _solo_label(_HIT_FOOTPRINT.values)
    'hit'
    """
    for footprint in FOOTPRINTS:
        if signature == footprint.values:
            return footprint.label
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
        label = _solo_label(signature)
        if label is None:
            return None, "none", "count_mismatch"
        return label, "solo", "none"

    label = _unanimous_label(signature, launch_count)
    if label is not None:
        return label, "unanimous", "none"
    return None, "none", "mixed_window"
