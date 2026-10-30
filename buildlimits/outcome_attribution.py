"""Attribute sccache counter movements only when a whole window agrees."""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_footprints import _COUNTER_FIELDS, _classify_delta

# The approved table is part of this module's public surface even though the
# rows now live beside the rules that read them, so callers keep importing it
# from the module that documents it.
from buildlimits.outcome_footprints import FOOTPRINTS as FOOTPRINTS

if typ.TYPE_CHECKING:
    from buildlimits.outcome_stats import StatsReading
    from buildlimits.outcome_vocabulary import CacheResult, OutcomeReason


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


def _counters_decreased(before: StatsReading, after: StatsReading) -> bool:
    """Check whether any cumulative counter moved backwards.

    Returns
    -------
    bool
        Whether a later reading is below its corresponding earlier value.

    Examples
    --------
    >>> from buildlimits.outcome_stats import StatsReading
    >>> empty = StatsReading(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "0.14.0")
    >>> _counters_decreased(empty, empty)
    False
    """
    return any(
        after_value < before_value
        for before_value, after_value in zip(
            _counter_values(before), _counter_values(after), strict=True
        )
    )


def _reading_failure(before: Reading, after: Reading) -> OutcomeReason | None:
    """Return the first failure that makes the two readings incomparable.

    Returns
    -------
    OutcomeReason or None
        The reading-level failure, or ``None`` when the counters are comparable.

    Examples
    --------
    >>> generation = Generation("run", 1, 2)
    >>> from buildlimits.outcome_stats import StatsReading
    >>> empty = StatsReading(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "0.14.0")
    >>> _reading_failure(Reading(generation, empty), Reading(None, empty))
    'generation_unavailable'
    """
    if before.generation is None or after.generation is None:
        return "generation_unavailable"
    if before.generation != after.generation:
        return "generation_changed"
    if all(value == 0 for value in _counter_values(after.stats)):
        return "empty_stats"
    if _counters_decreased(before.stats, after.stats):
        return "counter_decreased"
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
    failure = _reading_failure(before, after)
    if failure is None and any(not entry.is_clean for entry in window):
        return "ineligible_window"
    return failure


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
