"""Properties of the gauge accumulator's integral, peaks and cadence.

The examples in :mod:`tests.test_metrics_gauges` pin the contract; these
properties state the same invariants over generated sample timelines, which is
where the mistakes hide: an out-of-order reading folded into the integral, a
peak that resets at the wrong boundary, a gap that is silently integrated, or a
retry that loses the interval it replaces.

The references are written from the definitions rather than by calling the code
under test. The integral is a sum of left rectangles over the readings the
accumulator accepts, and a peak is the maximum over the readings since the last
delivered record.
"""

import dataclasses as dc
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from buildlimits.metrics_gauges import (
    GAUGE_INTERVAL_SECONDS,
    GAUGE_MIN_SPACING_SECONDS,
    GAUGE_STEP,
    GaugeAccumulator,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

MAX_DEPTH = 50

# Timestamps stay inside an hour, so the float products stay exact enough for
# the reference integral to be compared by equality rather than a tolerance.
SECONDS = st.floats(
    min_value=0.0, max_value=3_600.0, allow_nan=False, allow_infinity=False
)
DEPTHS = st.integers(min_value=0, max_value=MAX_DEPTH)

type Reading = tuple[float, int | None]


def _snapshot(queued: int, held: int) -> dict[str, object]:
    """Build the supervisor-wide values one sample carries.

    Parameters
    ----------
    queued : int
        Requests waiting for a compiler slot.
    held : int
        Tokens the supervisor holds.

    Returns
    -------
    dict[str, object]
        The snapshot the accumulator reads.

    Examples
    --------
    >>> _snapshot(2, 1)
    {'queued': 2, 'held': 1}
    """
    return {"queued": queued, "held": held}


def _reference_integral(readings: cabc.Iterable[Reading]) -> float:
    """Sum the left rectangles over the readings the accumulator accepts.

    A reading of a moment already integrated is not an interval, and an unread
    depth ends the run the previous depth would otherwise extend. Neither
    contributes a rectangle.

    Parameters
    ----------
    readings : Iterable[tuple[float, int | None]]
        Each ``(monotonic seconds, queue depth)`` pair in arrival order.

    Returns
    -------
    float
        The integral the interval should publish.
    """
    total = 0.0
    previous: Reading | None = None
    for at, depth in readings:
        if previous is not None:
            previous_at, previous_depth = previous
            if at < previous_at:
                continue
            if previous_depth is not None:
                total += previous_depth * (at - previous_at)
        previous = None if depth is None else (at, depth)
    return total


@given(readings=st.lists(st.tuples(SECONDS, DEPTHS), max_size=20))
@settings(max_examples=200, deadline=None)
def test_the_integral_is_a_non_decreasing_fold_over_accepted_time(
    readings: list[tuple[float, int]],
) -> None:
    """Out-of-order readings add nothing and the integral never falls."""
    accumulator = GaugeAccumulator()
    published = 0.0
    timeline: list[Reading] = []
    for at, depth in readings:
        timeline.append((at, depth))
        accumulator.observe(_snapshot(depth, 0), at)
        assert accumulator.queued_seconds >= published, (
            "an observation can only add non-negative area"
        )
        published = accumulator.queued_seconds
    assert accumulator.queued_seconds == pytest.approx(_reference_integral(timeline)), (
        "the fold matches the left rectangles over non-decreasing time"
    )


@given(
    intervals=st.lists(
        st.lists(st.tuples(SECONDS, DEPTHS, DEPTHS), min_size=1, max_size=6),
        min_size=1,
        max_size=4,
    )
)
@settings(max_examples=100, deadline=None)
def test_a_record_peaks_over_every_reading_since_the_last_one(
    intervals: list[list[tuple[float, int, int]]],
) -> None:
    """A peak rises between boundaries and restarts at the delivered record."""
    accumulator = GaugeAccumulator()
    anchor: float | None = None
    queued_peak: int | None = None
    held_peak: int | None = None
    high_water = 0
    now = 0.0
    snapshot = _snapshot(0, 0)
    for readings in intervals:
        for advance, queued, held in readings:
            now += advance
            snapshot = _snapshot(queued, held)
            accumulator.observe(snapshot, now)
            anchor = now if anchor is None else anchor
            queued_peak = queued if queued_peak is None else max(queued_peak, queued)
            held_peak = held if held_peak is None else max(held_peak, held)
            high_water = max(high_water, queued)
        record = accumulator.record(snapshot, now)
        assert record["queued_peak"] == queued_peak, "the interval peak is folded"
        assert record["held_peak"] == held_peak, "the held peak is folded"
        assert record["queued_high_water"] == high_water, "the maximum never falls"
        # Every interval begins with at least one generated sample, so the
        # origin is known by the time its record is shaped.
        assert anchor is not None, "every interval begins with a generated reading"
        assert record["interval_seconds"] == pytest.approx(now - anchor), (
            "the record claims the time since the interval began"
        )
        accumulator.settle(snapshot, now, accepted=True)
        # A delivered record starts the next interval from the values it
        # carried, so the peak is its own depth until a reading exceeds it.
        anchor = now
        queued_peak = typ.cast("int", snapshot["queued"])
        held_peak = typ.cast("int", snapshot["held"])


FOLLOW_ON_SECONDS = 10.0
FOLLOW_ON_DEPTH = 3


@given(
    readings=st.lists(
        st.tuples(SECONDS, st.one_of(st.none(), DEPTHS)),
        max_size=6,
    )
)
@settings(max_examples=150, deadline=None)
def test_an_unread_sample_removes_the_interval_mean(
    readings: list[tuple[float, int | None]],
) -> None:
    """A gap is never integrated, and the next interval does not inherit it."""
    accumulator = GaugeAccumulator()
    now = 0.0
    timeline: list[Reading] = []
    for advance, depth in readings:
        now += advance
        timeline.append((now, depth))
        accumulator.observe(None if depth is None else _snapshot(depth, 0), now)
    # Close on a real reading, so the interval has a measured endpoint and the
    # next interval can be measured even when the generated gap came before it.
    accumulator.observe(_snapshot(FOLLOW_ON_DEPTH, 0), now)
    record = accumulator.record(_snapshot(FOLLOW_ON_DEPTH, 0), now)
    if any(depth is None for _, depth in timeline):
        assert "queued_seconds" not in record, (
            "an interval with an unread depth publishes no mean at all"
        )
    else:
        assert record["queued_seconds"] == pytest.approx(
            _reference_integral(timeline)
        ), "a fully read interval reports its rectangles"
    accumulator.settle(_snapshot(FOLLOW_ON_DEPTH, 0), now, accepted=True)

    later = now + FOLLOW_ON_SECONDS
    accumulator.observe(_snapshot(FOLLOW_ON_DEPTH, 0), later)
    retried = accumulator.record(_snapshot(FOLLOW_ON_DEPTH, 0), later)
    assert retried["queued_seconds"] == pytest.approx(
        FOLLOW_ON_DEPTH * FOLLOW_ON_SECONDS
    ), "the gap is dropped, not carried into the interval that follows"


@given(
    baseline=st.tuples(DEPTHS, DEPTHS),
    steps=st.lists(st.tuples(SECONDS, DEPTHS, DEPTHS), max_size=12),
)
@settings(max_examples=150, deadline=None)
def test_a_record_is_due_on_the_heartbeat_or_a_stepped_count(
    baseline: tuple[int, int],
    steps: list[tuple[float, int, int]],
) -> None:
    """Cadence alone decides when the next record may be written."""
    accumulator = GaugeAccumulator()
    delivered = _snapshot(*baseline)
    accumulator.observe(delivered, 0.0)
    accumulator.settle(delivered, 0.0, accepted=True)
    flushed = 0.0
    stepped = False
    now = 0.0
    for advance, queued, held in steps:
        now += advance
        snapshot = _snapshot(queued, held)
        accumulator.observe(snapshot, now)
        stepped = stepped or any(
            abs(typ.cast("int", snapshot[name]) - typ.cast("int", delivered[name]))
            >= GAUGE_STEP
            for name in ("queued", "held")
        )
        elapsed = now - flushed
        due = elapsed >= GAUGE_INTERVAL_SECONDS or (
            stepped and elapsed >= GAUGE_MIN_SPACING_SECONDS
        )
        assert accumulator.is_due(now) is due, (
            "only the heartbeat or a step-spaced change makes a record due"
        )
        assert accumulator.is_due(now, force=True), "a forced record is always due"
        if due:
            accumulator.settle(snapshot, now, accepted=True)
            delivered, flushed, stepped = snapshot, now, False


def _read_through(
    accumulator: GaugeAccumulator,
    readings: list[tuple[float, int, int]],
    now: float,
    timeline: list[Reading],
) -> float:
    """Observe each reading in turn, recording what the reference saw.

    Parameters
    ----------
    accumulator : GaugeAccumulator
        The accumulator under test.
    readings : list[tuple[float, int, int]]
        One ``(advance, queued, held)`` triple per reading.
    now : float
        The instant reached before the first reading.
    timeline : list[Reading]
        The reference timeline, extended in place.

    Returns
    -------
    float
        The instant reached after the last reading.
    """
    for advance, queued, held in readings:
        now += advance
        timeline.append((now, queued))
        accumulator.observe(_snapshot(queued, held), now)
    return now


@given(
    before=st.lists(st.tuples(SECONDS, DEPTHS, DEPTHS), min_size=1, max_size=5),
    refused=st.tuples(DEPTHS, DEPTHS),
    after=st.lists(st.tuples(SECONDS, DEPTHS, DEPTHS), max_size=5),
    final=st.tuples(DEPTHS, DEPTHS),
)
@settings(max_examples=100, deadline=None)
def test_a_refused_record_keeps_the_interval_it_replaces(
    before: list[tuple[float, int, int]],
    refused: tuple[int, int],
    after: list[tuple[float, int, int]],
    final: tuple[int, int],
) -> None:
    """A lost record's peaks and area reach the next record that lands."""
    accumulator = GaugeAccumulator()
    # The delivered record is the first reading of the interval it opens, so
    # its own depth is part of the peak and the left endpoint of the integral.
    timeline: list[Reading] = [(0.0, 1)]
    accumulator.observe(_snapshot(1, 0), 0.0)
    accumulator.settle(_snapshot(1, 0), 0.0, accepted=True)
    now = _read_through(accumulator, before, 0.0, timeline)
    accumulator.settle(_snapshot(*refused), now, accepted=False)
    assert not accumulator.is_due(now), "the spacing defers the first retry"
    now = _read_through(accumulator, after, now, timeline)
    # Advance a whole heartbeat, so the retry is due whatever the counts did.
    now += GAUGE_INTERVAL_SECONDS
    timeline.append((now, final[0]))
    accumulator.observe(_snapshot(*final), now)
    assert accumulator.is_due(now), "the heartbeat makes the retry due"

    peak = max(depth for _, depth in timeline if depth is not None)
    record = accumulator.record(_snapshot(*final), now)
    assert record["queued_seconds"] == pytest.approx(_reference_integral(timeline)), (
        "the integral covers every interval since the delivered record"
    )
    assert record["queued_peak"] == peak, (
        "the peak covers every reading the refused record saw"
    )
    assert record["queued_high_water"] == peak, "the maximum never falls"
    assert record["interval_seconds"] == pytest.approx(now), (
        "the record claims the whole span since the delivered record"
    )


def test_a_refused_record_leaves_the_open_interval_untouched() -> None:
    """One lost record contributes its area and peaks to the next one."""
    accumulator = GaugeAccumulator()
    accumulator.observe(_snapshot(2, 0), 0.0)
    accumulator.settle(_snapshot(2, 0), 0.0, accepted=True)
    accumulator.observe(_snapshot(2, 0), 10.0)
    assert accumulator.record(_snapshot(2, 0), 10.0)["queued_seconds"] == pytest.approx(
        20.0
    ), "the refused record carried ten seconds of two requests"
    accumulator.settle(_snapshot(2, 0), 10.0, accepted=False)

    accumulator.observe(_snapshot(6, 1), 20.0)
    accumulator.observe(_snapshot(6, 1), 40.0)
    record = accumulator.record(_snapshot(6, 1), 40.0)
    assert record["queued_seconds"] == pytest.approx(2 * 10 + 2 * 10 + 6 * 20), (
        "the refused interval's twenty request-seconds are not lost"
    )
    assert record["queued_peak"] == 6, "the peak covers the readings after it"
    assert record["held_peak"] == 1, "the held peak covers them too"
    assert record["interval_seconds"] == pytest.approx(40.0), (
        "the record still covers the whole span"
    )


def test_is_due_and_record_do_not_mutate_the_accumulator() -> None:
    """Deciding and shaping a record are questions, not transitions."""
    accumulator = GaugeAccumulator()
    accumulator.observe(_snapshot(3, 1), 0.0)
    accumulator.settle(_snapshot(3, 1), 0.0, accepted=True)
    accumulator.observe(_snapshot(7, 2), 10.0)
    before = dc.asdict(accumulator)

    assert accumulator.is_due(10.0), (
        "the queue grew by a whole step, outside the minimum spacing"
    )
    assert accumulator.is_due(1_000.0, force=True), "a forced record ignores spacing"
    record = accumulator.record(_snapshot(7, 2), 10.0)
    assert record["queued_peak"] == 7, "the record still reflects the interval"
    assert dc.asdict(accumulator) == before, (
        "neither is_due nor record changes any field"
    )
