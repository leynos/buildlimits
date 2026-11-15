"""Periodic gauge snapshots: cadence, peaks, integrals and failure isolation.

These cases drive the accumulator with a hand-held clock, so no test waits on
wall-clock time, and pin the contract the supervisor's tick, admission and
shutdown paths rely on.
"""

import dataclasses as dc

import pytest
from metrics_support import unavailable_snapshot

from buildlimits.metrics import MAX_COUNTER
from buildlimits.metrics_gauges import (
    GAUGE_INTERVAL_SECONDS,
    GAUGE_MIN_SPACING_SECONDS,
    GAUGE_STEP,
    GaugeAccumulator,
)
from buildlimits.metrics_pressure import PressureMetrics

type Sample = tuple[float, int | None]


class RecordingPort:
    """Keep emitted events and let a test fail delivery on demand."""

    def __init__(self) -> None:
        """Accept records until a test says otherwise."""
        self.records: list[tuple[str, dict[str, object]]] = []
        self.accepts = True

    def emit(self, event: str, **fields: object) -> bool:
        """Record one event or refuse it.

        Returns
        -------
        bool
            Whether the event was kept.
        """
        if not self.accepts:
            return False
        self.records.append((event, fields))
        return True


def _gauges(port: RecordingPort) -> list[dict[str, object]]:
    """Return the fields of every ``gauges`` record a port received.

    Returns
    -------
    list[dict[str, object]]
        Field mappings in emission order.
    """
    return [fields for event, fields in port.records if event == "gauges"]


@dc.dataclass(slots=True)
class Rig:
    """A supervisor observer driven by a hand-held clock.

    Parameters
    ----------
    port : RecordingPort
        Records everything the observer emits.
    state : dict[str, object]
        Mutable snapshot the observer reads at each sample.
    now : list[float]
        One-element clock; tests move it without waiting.
    """

    port: RecordingPort
    state: dict[str, object]
    now: list[float]
    observer: PressureMetrics = dc.field(init=False)

    def __post_init__(self) -> None:
        """Bind the observer to this rig's snapshot and clock."""
        self.observer = PressureMetrics(
            self.port, lambda: self.state, clock=lambda: self.now[0]
        )

    @classmethod
    def start(cls, state: dict[str, object]) -> Rig:
        """Begin at zero seconds.

        Returns
        -------
        Rig
            A rig over a copy of *state*.
        """
        return cls(RecordingPort(), dict(state), [0.0])

    def advance(self, seconds: float) -> None:
        """Move the held clock forward without waiting."""
        self.now[0] += seconds

    def observe(self) -> None:
        """Fold the current snapshot into the open interval."""
        self.observer.observe(self.now[0])

    def flush(self, *, force: bool = False) -> bool:
        """Publish the open interval when it is due.

        Returns
        -------
        bool
            Whether a record reached the port.
        """
        return self.observer.flush(self.now[0], force=force)

    def baseline(self) -> None:
        """Observe and publish the zero-interval first record."""
        self.observe()
        assert self.flush(), "the first record is always due"

    def refuse_then_retry(self, seconds: float) -> bool:
        """Refuse one record, prove the spacing defers it, then retry.

        Parameters
        ----------
        seconds : float
            Time to advance before the retry; at least the minimum spacing.

        Returns
        -------
        bool
            Whether the retried record reached the port.
        """
        self.port.accepts = False
        assert not self.flush(), "the record must be refused for this case"
        assert not self.flush(), "the spacing defers an immediate retry"
        self.port.accepts = True
        self.advance(seconds)
        self.observe()
        return self.flush()

    def record(self, index: int) -> dict[str, object]:
        """Read one published ``gauges`` record.

        Returns
        -------
        dict[str, object]
            Fields of the record at *index*.
        """
        return _gauges(self.port)[index]


def test_baseline_anchors_peaks_and_the_lifetime_maximum() -> None:
    """The first record claims no interval and anchors every later maximum."""
    rig = Rig.start({"queued": 2, "held": 1})
    rig.baseline()
    rig.state["queued"] = 1
    rig.observe()
    record = rig.record(0)
    assert record["queued"] == 2, "the closing sample is reported"
    assert record["queued_peak"] == 2, "the peak starts at the sample"
    assert record["held_peak"] == 1, "the held peak starts at its sample"
    assert record["queued_high_water"] == 2, "the maximum never falls"
    assert record["interval_seconds"] == pytest.approx(0.0), "no interval yet"
    assert record["queued_seconds"] == pytest.approx(0.0), "no integral yet"


def test_lifetime_maximum_saturates_at_the_schema_bound() -> None:
    """A count beyond the schema bound never inflates the accumulator."""
    rig = Rig.start({"queued": MAX_COUNTER + 5})
    rig.baseline()
    record = rig.record(0)
    assert record["queued_peak"] == MAX_COUNTER, "the interval peak is capped"
    assert record["queued_high_water"] == MAX_COUNTER, "the maximum is capped"


@pytest.mark.parametrize(
    ("state", "samples", "expected"),
    [
        pytest.param(
            {"queued": 0, "held": 0},
            [(10.0, 4), (20.0, 2)],
            {"interval": 30.0, "queued_seconds": 80.0, "peak": 4},
            id="unequal-gaps",
        ),
        pytest.param(
            {},
            [(10.0, None), (10.0, None), (10.0, None)],
            {"interval": 30.0, "queued_seconds": None, "peak": None},
            id="unreadable-depth",
        ),
        pytest.param(
            {},
            [(10.0, 3), (10.0, None), (10.0, 1)],
            {"interval": 30.0, "queued_seconds": None, "peak": 3},
            id="gap-excluded",
        ),
    ],
)
def test_interval_accounting_follows_the_sample_timeline(
    state: dict[str, object],
    samples: list[Sample],
    expected: dict[str, float | None],
) -> None:
    """Depth is integrated as piecewise constant; gaps stay out of the sum."""
    rig = Rig.start(state)
    rig.baseline()
    for advance, depth in samples:
        rig.advance(advance)
        if depth is None:
            rig.state.pop("queued", None)
        else:
            rig.state["queued"] = depth
        rig.observe()
    assert rig.flush(), "the heartbeat interval has elapsed"
    record = rig.record(1)
    assert record["interval_seconds"] == pytest.approx(expected["interval"]), (
        "the record covers the wall interval since the last delivery"
    )
    if expected["queued_seconds"] is None:
        assert "queued_seconds" not in record, (
            "an interval with unread depth claims no measured mean"
        )
    else:
        assert record["queued_seconds"] == pytest.approx(expected["queued_seconds"]), (
            "only fully readable intervals publish an integral"
        )
    assert record.get("queued_peak") == expected["peak"], (
        "an unreadable interval publishes no peak"
    )


def test_an_earlier_observation_never_rewinds_the_integral() -> None:
    """A service poll may sample later than the tick reading that follows it."""
    accumulator = GaugeAccumulator()
    for now in (0.0, 10.0, 5.0, 20.0):
        accumulator.observe({"queued": 2}, now)
    record = accumulator.record({"queued": 2}, 20.0)
    assert record["interval_seconds"] == pytest.approx(20.0), (
        "the interval still spans the whole observation timeline"
    )
    assert record["queued_seconds"] == pytest.approx(40.0), (
        "the five-to-ten-second segment is integrated exactly once"
    )


@pytest.mark.parametrize(
    ("depth", "early"),
    [
        pytest.param(GAUGE_STEP, True, id="whole-step"),
        pytest.param(GAUGE_STEP - 1, False, id="below-step"),
    ],
)
def test_step_changes_earn_a_record_only_after_the_minimum_spacing(
    depth: int, *, early: bool
) -> None:
    """The spacing gates a step record; the heartbeat still fires at the interval."""
    rig = Rig.start({"queued": 0, "held": 0})
    rig.baseline()
    rig.advance(2.0)
    rig.state["queued"] = depth
    rig.observe()
    assert not rig.flush(), "the spacing refuses a record at two seconds"
    rig.advance(GAUGE_MIN_SPACING_SECONDS - 2.0)
    rig.observe()
    assert rig.flush() is early, "only a whole step earns the early record"
    if not early:
        rig.advance(GAUGE_INTERVAL_SECONDS - GAUGE_MIN_SPACING_SECONDS)
        rig.observe()
        assert rig.flush(), "the heartbeat is due at the interval"


def test_failed_emission_merges_peaks_and_integral_into_the_next_record() -> None:
    """A lost record leaves its interval open for the next attempt."""
    rig = Rig.start({"queued": 0, "held": 0})
    rig.baseline()
    for advance, depth in [(10.0, 2), (10.0, 5), (10.0, 1)]:
        rig.advance(advance)
        rig.state["queued"] = depth
        rig.observe()
    assert rig.refuse_then_retry(10.0), "the next interval publishes both"
    record = rig.record(1)
    assert record["interval_seconds"] == pytest.approx(40.0), (
        "the interval spans the failed attempt"
    )
    assert record["queued_seconds"] == pytest.approx(80.0), (
        "depth 2 for 10 s, then 5 for 10 s, then 1 for 20 s"
    )
    assert record["queued_peak"] == 5, "the lost interval keeps its peak"


def test_a_refused_baseline_claims_the_interval_it_integrated() -> None:
    """The retried first record reports the time its retained integral covers."""
    rig = Rig.start({"queued": 2, "held": 0})
    rig.observe()
    assert rig.refuse_then_retry(GAUGE_MIN_SPACING_SECONDS), (
        "the retried baseline follows the spacing"
    )
    record = rig.record(0)
    assert record["interval_seconds"] == pytest.approx(GAUGE_MIN_SPACING_SECONDS), (
        "the baseline claims the interval it integrated rather than zero"
    )
    assert record["queued_seconds"] == pytest.approx(2 * GAUGE_MIN_SPACING_SECONDS), (
        "the area the retry carries is the area it claims"
    )


def test_a_refused_baseline_waits_for_the_retry_spacing() -> None:
    """A missing first record is not retried on every tick."""
    rig = Rig.start({"queued": 1})
    rig.observe()
    rig.port.accepts = False
    assert not rig.flush(), "the baseline was refused"
    rig.port.accepts = True
    rig.advance(GAUGE_MIN_SPACING_SECONDS / 2)
    rig.observe()
    assert not rig.flush(), "the spacing defers the first retry as well"
    rig.advance(GAUGE_MIN_SPACING_SECONDS / 2)
    rig.observe()
    assert rig.flush(), "the baseline is due once the spacing has passed"


def test_forced_flush_publishes_before_the_heartbeat() -> None:
    """Shutdown writes the final interval without waiting for the cadence."""
    rig = Rig.start({"queued": 1, "held": 0})
    rig.baseline()
    rig.advance(1.0)
    rig.state["queued"] = 2
    rig.observe()
    assert not rig.flush(), "the cadence is not due yet"
    assert rig.flush(force=True), "shutdown forces the record"
    assert rig.record(1)["interval_seconds"] == pytest.approx(1.0), (
        "the final interval is short but whole"
    )


def test_forwarded_events_keep_exactly_their_producer_fields() -> None:
    """The port no longer enriches other events with supervisor gauges."""
    rig = Rig.start({"queued": 2, "held": 1})
    assert rig.observer.emit("supervisor_started", health="healthy"), "record took"
    assert rig.port.records == [("supervisor_started", {"health": "healthy"})], (
        "no gauge is added to the event"
    )
    assert not _gauges(rig.port), "forwarding does not publish a snapshot"


def test_snapshot_failure_is_isolated_from_emission() -> None:
    """An unreadable snapshot neither drops events nor invents a record."""
    port = RecordingPort()
    observer = PressureMetrics(port, unavailable_snapshot)
    assert observer.emit("pool_failure", operation="read"), "event still forward"
    assert port.records == [("pool_failure", {"operation": "read"})], (
        "the event carries only its producer's field"
    )
    observer.observe(0.0)
    assert not observer.flush(0.0), "no snapshot means no record"


def test_the_accumulator_defaults_match_the_documented_cadence() -> None:
    """The cadence, step and spacing stay fixed rather than configurable."""
    accumulator = GaugeAccumulator()
    assert accumulator.interval == pytest.approx(GAUGE_INTERVAL_SECONDS), (
        "the configured cadence is the default"
    )
    assert accumulator.interval == pytest.approx(30.0), "heartbeat cadence"
    assert accumulator.step == GAUGE_STEP, "step is the documented default"
    assert accumulator.step == 4, "step change is four counts"
    assert accumulator.min_spacing == pytest.approx(GAUGE_MIN_SPACING_SECONDS), (
        "the configured spacing is the default"
    )
    assert accumulator.min_spacing == pytest.approx(5.0), "minimum spacing"
