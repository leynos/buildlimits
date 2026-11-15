"""Cached pressure snapshots published only on periodic gauge records."""

import pytest
from conftest import RecordingSink
from hypothesis import given
from hypothesis import strategies as st
from metrics_support import unavailable_snapshot

from buildlimits.config import SupervisorConfig
from buildlimits.metrics import (
    MAX_COUNTER,
    VOCABULARIES,
    Metrics,
    decode_native_fields,
)
from buildlimits.metrics_gauges import GAUGE_INTERVAL_SECONDS
from buildlimits.metrics_pressure import PressureMetrics
from buildlimits.supervisor import Supervisor


class FailingPort:
    """Reject observations to test independence from the underlying operation."""

    @staticmethod
    def emit(event: str, **_fields: object) -> bool:
        """Raise a private error whose details must not leave the observer."""
        del event
        msg = "private transport failure /secret/path"
        raise RuntimeError(msg)


def _flush(
    observer: PressureMetrics, sink: RecordingSink, now: float
) -> dict[str, str]:
    """Publish one forced snapshot and return the native record.

    Returns
    -------
    dict[str, str]
        The record the sink received.
    """
    observer.observe(now)
    assert observer.flush(now, force=True), "the forced snapshot reached the sink"
    return sink.records[-1]


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
        observer = supervisor_metrics(instance, Metrics(metric_sink))
        observer.observe(0.0)
        assert observer.flush(0.0, force=True), "the baseline snapshot is published"
        record = metric_sink.records[-1]
        assert record["BUILD_LIMITS_EVENT"] == "gauges", "one periodic record"
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
    sink = RecordingSink()
    state: dict[str, object] = {"queued": 0}
    now = [0.0]
    observer = PressureMetrics(Metrics(sink), lambda: state, clock=lambda: now[0])
    maxima = []
    for depth in depths:
        state["queued"] = depth
        now[0] += 1.0
        record = _flush(observer, sink, now[0])
        maxima.append(int(record["BUILD_LIMITS_QUEUED_HIGH_WATER"]))
    expected = [
        min(MAX_COUNTER, max(depths[: index + 1])) for index in range(len(depths))
    ]
    assert maxima == expected, "lifetime maximum saturates instead of resetting"


def test_a_lost_snapshot_keeps_its_maximum_for_the_next_record(metric_sink) -> None:
    """A refused record leaves the interval open instead of erasing it."""

    class FailOnce:
        """Refuse the first record and forward every later one."""

        def __init__(self) -> None:
            """Start with one fault remaining."""
            self.failed = False

        def emit(self, event: str, **fields: object) -> bool:
            """Model one transport fault before using the validating emitter."""
            if not self.failed:
                self.failed = True
                msg = "private failure"
                raise OSError(msg)
            return Metrics(metric_sink).emit(event, **fields)

    now = [0.0]
    state: dict[str, object] = {"queued": 8}
    observer = PressureMetrics(FailOnce(), lambda: state, clock=lambda: now[0])
    observer.observe(now[0])
    assert not observer.flush(now[0]), "the first attempt is lost"
    now[0] = 5.0
    state["queued"] = 0
    kept = _flush(observer, metric_sink, now[0])
    assert kept["BUILD_LIMITS_QUEUED_HIGH_WATER"] == "8", (
        "the lost interval still counts towards the lifetime maximum"
    )
    fresh = PressureMetrics(Metrics(metric_sink), lambda: state, clock=lambda: now[0])
    reset = _flush(fresh, metric_sink, now[0])
    assert reset["BUILD_LIMITS_QUEUED_HIGH_WATER"] == "0", (
        "a new supervisor observer resets its lifetime maximum"
    )


def test_a_failed_endpoint_sample_leaves_the_interval_open(metric_sink) -> None:
    """A read failure at the flush endpoint must not settle a stale interval."""
    now = [0.0]
    state: dict[str, object] = {"queued": 2}
    readable = [True]

    def snapshot() -> dict[str, object]:
        """Model a cached-state reader that fails on demand."""
        if not readable[0]:
            msg = "private cached-state fault"
            raise RuntimeError(msg)
        return state

    observer = PressureMetrics(Metrics(metric_sink), snapshot, clock=lambda: now[0])
    baseline = _flush(observer, metric_sink, now[0])
    assert baseline["BUILD_LIMITS_EVENT"] == "gauges", "the baseline reached the sink"
    readable[0] = False
    now[0] = GAUGE_INTERVAL_SECONDS
    observer.observe(now[0])
    assert not observer.flush(now[0]), "no record may end at an unread endpoint"
    readable[0] = True
    now[0] = 2 * GAUGE_INTERVAL_SECONDS
    observer.observe(now[0])
    assert observer.flush(now[0]), "the next readable endpoint publishes both"
    record = metric_sink.records[-1]
    assert record["BUILD_LIMITS_INTERVAL_SECONDS"] == "60.000", (
        "the published interval spans the unread attempt"
    )
    assert record["BUILD_LIMITS_QUEUED_SECONDS"] == "120.000", (
        "the depth that waited through the unread endpoint is not lost"
    )


def test_the_cached_endpoint_keeps_the_freshest_sample(metric_sink) -> None:
    """A tick's earlier clock reading cannot overwrite a later poll sample."""
    observer = PressureMetrics(
        Metrics(metric_sink), lambda: {"queued": 2}, clock=lambda: 0.0
    )
    for sampled_at in (0.0, 10.0, 4.0):
        observer.observe(sampled_at)
    assert observer.flush(10.0), "the interval ends at the freshest sample"
    record = metric_sink.records[-1]
    assert record["BUILD_LIMITS_INTERVAL_SECONDS"] == "10.000", (
        "the cached endpoint never moves backwards"
    )
    assert record["BUILD_LIMITS_QUEUED_SECONDS"] == "20.000", (
        "the depth is integrated exactly once up to the freshest sample"
    )


def test_snapshot_failure_is_isolated_from_forwarding(metric_sink) -> None:
    """An unavailable observation cannot drop events or invent a record."""
    observer = PressureMetrics(Metrics(metric_sink), unavailable_snapshot)
    assert observer.emit("supervisor_started", health="healthy"), "event forwarded"
    assert len(metric_sink.records) == 1, "no gauge record accompanies it"
    assert metric_sink.records[0]["BUILD_LIMITS_EVENT"] == "supervisor_started", (
        "only the forwarded event was kept"
    )
    observer.observe(0.0)
    assert not observer.flush(0.0), "a missing sample has no interval to publish"


def test_transport_failure_never_escapes_the_observer() -> None:
    """A failing sink reports refusal without raising into the caller."""
    observer = PressureMetrics(FailingPort(), lambda: {"queued": 1}, clock=lambda: 0.0)
    assert not observer.emit("request_finished"), "the event was not delivered"
    observer.observe(0.0)
    assert not observer.flush(0.0), "the record was not delivered"


def test_gauge_records_keep_fail_closed_zero_and_unknown(metric_sink) -> None:
    """Present zeros and an unknown baseline stay explicit on the snapshot."""
    state: dict[str, object] = {
        "queued": 0,
        "fd_retained": 0,
        "fd_budget": 0,
        "fd_budget_known": False,
        "fd_high_water": 4,
        "accept_paused": True,
    }
    observer = PressureMetrics(Metrics(metric_sink), lambda: state, clock=lambda: 0.0)
    decoded = decode_native_fields(_flush(observer, metric_sink, 0.0))
    assert decoded["fd_budget"] == "0", "present effective zero is retained"
    assert decoded["fd_budget_known"] == "0", "unknown baseline remains explicit"
    assert decoded["accept_paused"] == "1", "listener state is a boolean"
    assert decoded["queued_high_water"] == "0", "the lifetime maximum is explicit"


def test_accept_pressure_events_still_drop_identifiers(metric_sink) -> None:
    """The existing validating schema removes identity and free text."""
    observer = PressureMetrics(Metrics(metric_sink), dict, clock=lambda: 0.0)
    observer.emit(
        "accept_pressure",
        accept_state="paused",
        accept_errno="emfile",
        accept_pause_seconds=0,
        path="/secret/path",
        request_id="private",
    )
    record = metric_sink.records[0]
    assert decode_native_fields(record)["accept_pause_seconds"] == "0.000", (
        "seconds round to milliseconds"
    )
    assert "private" not in str(record) and "secret" not in str(record), (
        "no value or identifier survives validation"
    )


@pytest.mark.parametrize("queued", [True, "10", -1, None])
def test_invalid_queue_sample_cannot_raise_high_water(
    metric_sink, queued: object
) -> None:
    """Invalid cached counters do not invent a lifetime maximum."""
    state: dict[str, object] = {"queued": queued}
    observer = PressureMetrics(Metrics(metric_sink), lambda: state, clock=lambda: 0.0)
    record = _flush(observer, metric_sink, 0.0)
    assert record["BUILD_LIMITS_QUEUED_HIGH_WATER"] == "0", (
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
