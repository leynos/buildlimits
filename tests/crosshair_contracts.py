"""Contracts that CrossHair checks symbolically over the pure decision functions.

Each function follows CrossHair's ``asserts`` convention: leading assertions
are preconditions and later assertions are postconditions. ``test_crosshair``
runs ``crosshair check`` over this module.
"""

from buildlimits.attributes import INFINITY, AttributeRefusalError, at_most, plan_limit
from buildlimits.outcome_attribution import (
    Generation,
    Reading,
    WindowEntry,
    attribute,
)
from buildlimits.outcome_stats import StatsReading

_MIN_UNANIMOUS_LAUNCHES = 2
_MAX_WINDOW_LAUNCHES = 4
_MIN_MIXED_FOOTPRINTS = 1


def limit_order_is_total(left: int, right: int) -> None:
    """Check that wire-form limits are totally ordered with infinity greatest."""
    assert left >= INFINITY, "wire-form limits never fall below infinity"
    assert right >= INFINITY, "wire-form limits never fall below infinity"
    assert at_most(left, right) or at_most(right, left), "any two limits are comparable"
    assert at_most(left, INFINITY), "infinity is the greatest limit"
    assert at_most(left, left), "a limit is at most itself"


def planned_limits_are_reachable(
    caller_soft: int, caller_hard: int, current_soft: int, current_hard: int
) -> None:
    """Check that a plan keeps the soft limit and never raises the hard one."""
    assert min(caller_soft, caller_hard, current_soft, current_hard) >= INFINITY, (
        "wire-form limits never fall below infinity"
    )
    assert at_most(caller_soft, caller_hard), "the caller's own limits are ordered"
    assert at_most(current_soft, current_hard), "the current limits are ordered"
    try:
        soft, hard = plan_limit(
            (caller_soft, caller_hard), (current_soft, current_hard)
        )
    except AttributeRefusalError:
        assert not at_most(caller_soft, current_hard), (
            "a refusal only happens when the caller's soft limit is unreachable"
        )
        return
    assert soft == caller_soft, "the plan keeps the caller's soft limit"
    assert at_most(hard, current_hard), "the plan never raises the hard limit"
    assert at_most(soft, hard), "the planned limits stay ordered"
    assert hard == caller_hard or not at_most(caller_hard, current_hard), (
        "the plan reaches the caller's hard limit whenever it is reachable"
    )


def unanimous_hit_windows_keep_the_hit_label(launch_count: int) -> None:
    """Prove every bounded unanimous hit window retains its own label.

    Examples
    --------
    >>> unanimous_hit_windows_keep_the_hit_label(2)
    """
    assert _MIN_UNANIMOUS_LAUNCHES <= launch_count <= _MAX_WINDOW_LAUNCHES, (
        "unanimity requires multiple launches"
    )
    generation = Generation("run", 1, 2)
    baseline = StatsReading(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "0.14.0")
    after = StatsReading(
        launch_count,
        0,
        0,
        0,
        launch_count,
        launch_count,
        0,
        0,
        0,
        0,
        0,
        "0.14.0",
    )
    window = tuple(WindowEntry("g", is_clean=True) for _ in range(launch_count))

    result = attribute(
        Reading(generation, baseline), Reading(generation, after), window
    )

    assert result.kind == "unanimous", "repeated hits have unanimous attribution"
    assert result.reason == "none", "repeated hits have no failure reason"
    assert all(label == "hit" for _, label in result.labels), (
        "each repeated hit keeps its label"
    )


def cache_errors_are_not_dropped_from_a_solo_footprint(error_count: int) -> None:
    """Prove a cache-only failure counter cannot become a successful label.

    Examples
    --------
    >>> cache_errors_are_not_dropped_from_a_solo_footprint(1)
    """
    assert error_count == 1, "the solo cache-error footprint has one error"
    generation = Generation("run", 1, 2)
    baseline = StatsReading(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "0.14.0")
    after = StatsReading(1, 0, 0, 0, 1, 0, 0, error_count, 0, 0, 0, "0.14.0")
    window = (WindowEntry("g0", is_clean=True),)

    result = attribute(
        Reading(generation, baseline), Reading(generation, after), window
    )

    assert result.labels == (("g0", "error"),), (
        "a solo cache error retains its error label"
    )
    assert result.kind == "solo", "one cache-error footprint is identifiable"
    assert result.reason == "none", "a solo cache error has no attribution failure"


def repeated_cache_errors_are_not_unanimous(launch_count: int) -> None:
    """Prove an error footprint never becomes a unanimous outcome class.

    Examples
    --------
    >>> repeated_cache_errors_are_not_unanimous(2)
    """
    assert _MIN_UNANIMOUS_LAUNCHES <= launch_count <= _MAX_WINDOW_LAUNCHES, (
        "a unanimous error requires several launches"
    )
    generation = Generation("run", 1, 2)
    baseline = StatsReading(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "0.14.0")
    after = StatsReading(
        launch_count,
        0,
        0,
        0,
        launch_count,
        0,
        0,
        launch_count,
        0,
        0,
        0,
        "0.14.0",
    )
    window = tuple(WindowEntry("g", is_clean=True) for _ in range(launch_count))

    result = attribute(
        Reading(generation, baseline), Reading(generation, after), window
    )

    assert result.kind == "none", "cache errors are not an approved unanimous class"
    assert result.reason == "mixed_window", (
        "repeated errors remain an unattributed mixture"
    )
    assert all(label == "unattributed" for _, label in result.labels), (
        "no launch in a repeated error window receives a label"
    )


def decoding_never_labels_mixed(hit_count: int) -> None:
    """Keep every bounded mixture of hits and misses unattributed.

    Examples
    --------
    >>> decoding_never_labels_mixed(2)
    """
    assert _MIN_MIXED_FOOTPRINTS <= hit_count < _MAX_WINDOW_LAUNCHES, (
        "a mixed window contains at least one hit and one miss"
    )
    miss_count = _MAX_WINDOW_LAUNCHES - hit_count
    generation = Generation("run", 1, 2)
    baseline = StatsReading(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "0.14.0")
    after = StatsReading(
        _MAX_WINDOW_LAUNCHES,
        0,
        0,
        0,
        _MAX_WINDOW_LAUNCHES,
        hit_count,
        miss_count,
        0,
        0,
        miss_count,
        0,
        "0.14.0",
    )
    window = (
        WindowEntry("g0", is_clean=True),
        WindowEntry("g1", is_clean=True),
        WindowEntry("g2", is_clean=True),
        WindowEntry("g3", is_clean=True),
    )

    result = attribute(
        Reading(generation, baseline), Reading(generation, after), window
    )

    assert result.kind == "none", "a hit and miss mixture has no attribution"
    assert result.reason == "mixed_window", "the balanced mixture is reported as mixed"
    assert len(result.labels) == _MAX_WINDOW_LAUNCHES, (
        "every group receives a fallback label"
    )
    assert all(label == "unattributed" for _, label in result.labels), (
        "mixed hits and misses are never split into individual labels"
    )
