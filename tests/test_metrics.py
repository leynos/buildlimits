"""Bounded vocabulary, numeric limits, failure isolation and secrecy."""

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from buildlimits.effects import Exit
from buildlimits.metrics import MAX_COUNTER, Metrics, exit_outcome, refusal_reason


def test_metrics_drop_individual_fields_without_leaking(metric_sink) -> None:
    """Names and values outside the contract never reach the transport."""
    warnings: list[str] = []
    emitter = Metrics(metric_sink, warn=warnings.append)
    emitter.emit(
        "request_finished",
        outcome="completed",
        wait_seconds=1.23456,
        queued=True,
        env="secret=/private/crate",
        reason="secret",
        **{"/private/token": "private", "_PID": 123},
    )
    record = metric_sink.records[0]
    assert record["BUILD_LIMITS_WAIT_SECONDS"] == "1.235", "milliseconds rounded"
    assert record["BUILD_LIMITS_OUTCOME"] == "completed", "valid fields preserved"
    assert emitter.dropped_fields == 5, "each bad field drops independently"
    assert len(warnings) == 1, "warning is bounded across failures"
    assert "private" not in str(record) + str(warnings), "secrets never leave emitter"


@pytest.mark.parametrize("value", [True, -1, math.nan, math.inf, "12", {}, None])
def test_invalid_durations_are_dropped(metric_sink, value: object) -> None:
    """Only finite non-negative numeric durations can be measured."""
    Metrics(metric_sink).emit("request_finished", runtime_seconds=value)
    assert "BUILD_LIMITS_RUNTIME_SECONDS" not in metric_sink.records[0], (
        "invalid dropped"
    )


@given(value=st.integers(min_value=0))
def test_counters_are_capped(value: int) -> None:
    """An arbitrarily large counter never exceeds the schema cap."""
    from buildlimits.metrics import bounded_field

    assert bounded_field("connection_attempts", value) == str(
        min(value, MAX_COUNTER)
    ), "bounded count"


@pytest.mark.parametrize(
    ("exit_", "expected"),
    [
        (Exit(code=0), "completed"),
        (Exit(code=9), "failed"),
        (Exit(signal=15), "signalled"),
        (Exit(), "unknown"),
    ],
)
def test_exit_classification(exit_: Exit, expected: str) -> None:
    """Compiler identity and raw status numbers are unnecessary labels."""
    assert exit_outcome(exit_) == expected, "ending classified"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("launch-failed", "launch_failed"),
        ("bad-request", "bad_request"),
        ("attributes", "attributes"),
        ("descriptors", "descriptors"),
        ("/secret/error", "other"),
    ],
)
def test_refusal_mapping(reason: str, expected: str) -> None:
    """Unknown reasons collapse to a single bounded category."""
    assert refusal_reason(reason) == expected, "refusal normalized"


def test_sink_and_warning_failures_are_isolated() -> None:
    """Neither transport nor diagnostic exceptions can escape emission."""

    class BrokenSink:
        """A transport that exposes an arbitrary failure."""

        def emit(self, record) -> None:
            """Fail without emitting."""
            msg = "secret raw transport error"
            raise RuntimeError(msg)

    warned: list[str] = []

    def broken_warning(message: str) -> None:
        """Fail as a broken diagnostic sink would."""
        warned.append(message)
        msg = "secret diagnostic error"
        raise RuntimeError(msg)

    emitter = Metrics(BrokenSink(), warn=broken_warning)
    emitter.emit("request_finished")
    emitter.emit("request_finished")
    emitter.emit("secret event")
    assert emitter.dropped_records == 3, "all lost records counted"
    assert len(warned) == 1, "only one warning attempted"
    assert "secret" not in warned[0], "warning never echoes rejected content"


def test_invalid_event_drops_entire_record(metric_sink) -> None:
    """A plausible-looking but unknown event is never sent."""
    emitter = Metrics(metric_sink)
    emitter.emit("request_custom", queued=1)
    assert not metric_sink.records, "invalid event drops everything"
    assert emitter.dropped_records == 1, "drop counted"


def test_gauge_snapshot_is_frozen() -> None:
    """A consumer cannot mutate admission through its gauge snapshot."""
    import dataclasses

    from buildlimits.admission import Admission

    counts = Admission().counts()
    name = dataclasses.fields(counts)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(counts, name, 8)
