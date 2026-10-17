"""Cached pressure snapshots and failure isolation at the existing metric port."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from buildlimits.config import SupervisorConfig
from buildlimits.metrics import (
    MAX_COUNTER,
    VOCABULARIES,
    Metrics,
    decode_native_fields,
)
from buildlimits.metrics_pressure import PressureMetrics
from buildlimits.supervisor import Supervisor


class FailingPort:
    """Reject observations to test independence from the underlying operation."""

    @staticmethod
    def emit(event: str, **_fields: object) -> None:
        """Raise a private error whose details must not leave the observer."""
        del event
        msg = "private transport failure /secret/path"
        raise RuntimeError(msg)


def test_real_supervisor_uses_cached_descriptor_snapshot(
    metric_sink, monkeypatch
) -> None:
    """The production observer reads the actual Requests descriptor-budget owner."""
    from buildlimits import descriptor_budget
    from buildlimits.metrics_pressure import supervisor_metrics

    instance = Supervisor(SupervisorConfig(), clock=lambda: 0.0)
    try:
        instance.requests.descriptors.budget = 0
        instance.requests.descriptors.baseline_known = False
        instance.requests.descriptors.high_water = 7
        instance.pressure.paused = True

        def forbidden_sample() -> None:
            """Fail if metric emission tries to resample the descriptor table."""
            msg = "emission must use cached producer observations"
            raise AssertionError(msg)

        monkeypatch.setattr(descriptor_budget, "open_count", forbidden_sample)
        supervisor_metrics(instance, Metrics(metric_sink)).emit("supervisor_started")
        record = metric_sink.records[-1]
        assert record["BUILD_LIMITS_FD_RETAINED"] == "0", "no requests are retained"
        assert record["BUILD_LIMITS_FD_BUDGET"] == "0", "effective zero is cached"
        assert record["BUILD_LIMITS_FD_BUDGET_KNOWN"] == "0", (
            "baseline unknown is explicit"
        )
        assert record["BUILD_LIMITS_FD_HIGH_WATER"] == "7", (
            "descriptor maximum is cached"
        )
        assert record["BUILD_LIMITS_ACCEPT_PAUSED"] == "1", "listener state is cached"
        assert record["BUILD_LIMITS_CAPACITY"] == str(instance.settings.slots), (
            "domain gauges share the same observation"
        )
    finally:
        instance.selector.close()


@given(depths=st.lists(st.integers(min_value=0), min_size=1, max_size=40))
def test_queue_high_water_tracks_lifetime(depths: list[int]) -> None:
    """Producer maxima remain monotonic across arbitrary queue fluctuations."""
    from conftest import RecordingSink

    sink = RecordingSink()
    state: dict[str, object] = {"queued": 0}
    observer = PressureMetrics(Metrics(sink), lambda: state)
    maxima = []
    for depth in depths:
        state["queued"] = depth
        observer.emit("request_queued")
        maxima.append(int(sink.records[-1]["BUILD_LIMITS_QUEUED_HIGH_WATER"]))
    expected = [
        min(MAX_COUNTER, max(depths[: index + 1])) for index in range(len(depths))
    ]
    assert maxima == expected, "lifetime maximum saturates instead of resetting"


def test_failed_emission_still_observes_queue_maximum(metric_sink) -> None:
    """A dropped record does not erase the actual producer high-water mark."""

    class FailOnce:
        """Fail the first observation and then forward remaining measurements."""

        def __init__(self) -> None:
            """Start with one fault remaining."""
            self.failed = False

        def emit(self, event: str, **fields: object) -> None:
            """Expose one port failure before using the validating emitter."""
            if not self.failed:
                self.failed = True
                msg = "private failure"
                raise OSError(msg)
            Metrics(metric_sink).emit(event, **fields)

    state: dict[str, object] = {"queued": 8}
    observer = PressureMetrics(FailOnce(), lambda: state)
    observer.emit("request_queued")
    state["queued"] = 0
    observer.emit("request_finished")
    assert metric_sink.records[0]["BUILD_LIMITS_QUEUED_HIGH_WATER"] == "8", (
        "queue high-water is observed before a sink fault"
    )
    fresh = PressureMetrics(Metrics(metric_sink), lambda: state)
    fresh.emit("supervisor_started")
    assert metric_sink.records[-1]["BUILD_LIMITS_QUEUED_HIGH_WATER"] == "0", (
        "a new supervisor observer resets its lifetime maximum"
    )


def test_snapshot_failure_is_isolated(metric_sink) -> None:
    """An unavailable observation cannot affect admission or emit partial gauges."""

    def unavailable() -> dict[str, object]:
        """Model a cached-state reader failure without resource sampling."""
        msg = "private cached-state fault"
        raise RuntimeError(msg)

    PressureMetrics(Metrics(metric_sink), unavailable).emit("supervisor_started")
    assert metric_sink.records == [], "failed snapshots are best-effort"
    PressureMetrics(FailingPort(), lambda: {"queued": 1}).emit("request_queued")


def test_pressure_zero_unknown_and_secrecy(metric_sink) -> None:
    """Fail-closed zero is present while unknown identifiers never reach the sink."""
    observer = PressureMetrics(
        Metrics(metric_sink),
        lambda: {
            "queued": 0,
            "fd_retained": 0,
            "fd_budget": 0,
            "fd_budget_known": False,
            "fd_high_water": 4,
            "accept_paused": True,
        },
    )
    observer.emit(
        "accept_pressure",
        accept_state="paused",
        accept_errno="emfile",
        accept_pause_seconds=0,
        path="/secret/path",
        request_id="private",
    )
    record = metric_sink.records[0]
    decoded = decode_native_fields(record)
    assert decoded["fd_budget"] == "0", "present effective zero is retained"
    assert decoded["fd_budget_known"] == "0", "unknown baseline remains explicit"
    assert decoded["accept_pause_seconds"] == "0.000", "seconds round to milliseconds"
    assert "private" not in str(record) and "secret" not in str(record), (
        "the existing validating schema removes identity and free text"
    )


@pytest.mark.parametrize("queued", [True, "10", -1, None])
def test_invalid_queue_sample_cannot_raise_high_water(
    metric_sink, queued: object
) -> None:
    """Invalid cached counters do not invent a lifetime maximum."""
    PressureMetrics(Metrics(metric_sink), lambda: {"queued": queued}).emit(
        "request_queued"
    )
    assert metric_sink.records[0]["BUILD_LIMITS_QUEUED_HIGH_WATER"] == "0", (
        "invalid queue samples never inflate high-water"
    )


@pytest.mark.parametrize(
    "name",
    [
        "accept_state",
        "accept_errno",
        "drain_operation",
        "drain_state",
        "drain_reason",
        # The recovery phase reaches the same boundary as the drain tokens, so
        # it is pinned here rather than only by the snapshot: a phase that fell
        # out of the vocabulary would be dropped by ``emit`` and leave a stuck
        # transaction invisible in the journal.
        "phase",
    ],
)
def test_pressure_tokens_round_trip_without_new_labels(metric_sink, name: str) -> None:
    """Each accepted pressure label comes only from its frozen vocabulary."""
    emitter = Metrics(metric_sink)
    for token in sorted(VOCABULARIES[name]):
        emitter.emit("drain_progress", **{name: token})
        assert decode_native_fields(metric_sink.records[-1])[name] == token, (
            "fixed tokens survive native decoding"
        )
    emitter.emit("drain_progress", **{name: "/private/identifier"})
    assert name not in decode_native_fields(metric_sink.records[-1]), (
        "unknown labels cannot escape the schema"
    )
