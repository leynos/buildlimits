"""Exact-once drain actions, fixed observations and fault recovery boundaries."""

import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from buildlimits import drain_metrics
from buildlimits.drain_metrics import DrainMetrics
from buildlimits.metrics import Metrics
from buildlimits.sccache import LiveCacheWorkError, SccacheError


def _raise(error: Exception) -> typ.Never:
    """Raise the supplied object to check transparent exception preservation."""
    raise error


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OSError("private path"), ("failed", "os_error", "1")),
        (SccacheError("private manager"), ("failed", "control_error", "1")),
        (LiveCacheWorkError("private live work"), ("pending", "live_work", "0")),
    ],
)
def test_action_error_identity_and_fixed_classification(
    metric_sink, error: Exception, expected: tuple[str, str, str]
) -> None:
    """Errors retain identity and cause while telemetry contains fixed tokens only."""
    state, reason, failures = expected
    observer = DrainMetrics(Metrics(metric_sink))
    cause = ValueError("private original cause")
    error.__cause__ = cause
    with pytest.raises(type(error)) as caught:
        observer.call("accounted_cache", lambda: _raise(error))
    assert caught.value is error and caught.value.__cause__ is cause, (
        "the action error and its original chain are unchanged"
    )
    record = metric_sink.records[0]
    assert record["BUILD_LIMITS_DRAIN_STATE"] == state, "fixed state classification"
    assert record["BUILD_LIMITS_DRAIN_REASON"] == reason, "fixed error category"
    assert record["BUILD_LIMITS_DRAIN_FAILURES"] == failures, "live work is not a fault"
    assert "private" not in str(record), "raw errors never become observations"


def test_pending_does_not_clear_fault(metric_sink) -> None:
    """A populated scope cannot claim probe recovery until a successful empty result."""
    observer = DrainMetrics(Metrics(metric_sink))
    with pytest.raises(OSError, match=r"^private$"):
        observer.call("empty_check", lambda: _raise(OSError("private")))
    assert observer.call("empty_check", lambda: True, pending=bool) is True, (
        "live scopes remain pending"
    )
    assert observer.call("empty_check", lambda: False, pending=bool) is False, (
        "empty result remains unchanged"
    )
    observer.call("empty_check", lambda: False, pending=bool)
    states = [record["BUILD_LIMITS_DRAIN_STATE"] for record in metric_sink.records]
    assert states == ["failed", "pending", "recovered", "completed"], (
        "only the first non-pending successful probe clears a fault"
    )
    assert metric_sink.records[-1]["BUILD_LIMITS_DRAIN_RECOVERIES"] == "1", (
        "one fault recovers once"
    )


def test_control_cycle_avoids_healthy_tick_spam(metric_sink) -> None:
    """Only faults and complete recovery cycles produce control-loop observations."""
    observer = DrainMetrics(Metrics(metric_sink))
    for _ in range(3):
        observer.control_cycle(lambda: None)
    assert metric_sink.records == [], "healthy select/dispatch/tick has no record"
    with pytest.raises(RuntimeError):
        observer.control_cycle(lambda: _raise(RuntimeError("private")))
    observer.call("control_loop", lambda: None, pending=lambda _: True)
    observer.control_cycle(lambda: None)
    observer.control_cycle(lambda: None)
    records = metric_sink.records
    assert [record["BUILD_LIMITS_DRAIN_STATE"] for record in records] == [
        "failed",
        "pending",
        "recovered",
    ], "fallback ticks are progress, never full-cycle recovery"
    assert records[-1]["BUILD_LIMITS_DRAIN_ATTEMPTS"] == "3", (
        "fault, fallback, and full recovery are attempts"
    )
    assert records[-1]["BUILD_LIMITS_DRAIN_RECOVERIES"] == "1", (
        "only the complete cycle recovers"
    )


def test_throwing_port_and_classifier_preserve_action(metric_sink) -> None:
    """Observer failures cannot change values, action count or exceptions."""
    from test_pressure_metrics import FailingPort

    calls: list[int] = []
    observer = DrainMetrics(FailingPort())
    result = object()
    assert observer.call("terminate", lambda: calls.append(1) or result) is result, (
        "port failure preserves result"
    )
    error = OSError("private")
    with pytest.raises(OSError, match=r"^private$") as caught:
        observer.call("terminate", lambda: _raise(error))
    assert caught.value is error, "emission failure does not replace the action fault"
    observer = DrainMetrics(Metrics(metric_sink))
    assert (
        observer.call(
            "empty_check",
            lambda: result,
            pending=lambda _: _raise(RuntimeError("private")),
        )
        is result
    ), "classification failure cannot change the action result"
    assert calls == [1], "the action executes exactly once"


def test_unknown_operations_execute_without_labels(metric_sink) -> None:
    """Arbitrary operations cannot enlarge the fixed observer state or leak identity."""
    observer = DrainMetrics(Metrics(metric_sink))
    result = object()
    assert observer.call("/private/scope", lambda: result) is result, (
        "unknown labels preserve operation"
    )
    assert metric_sink.records == [], "unknown operations do not emit a new label"
    assert len(observer._attempts) == 5, "state contains only the five fixed operations"


@given(history=st.lists(st.booleans(), min_size=1, max_size=30))
def test_drain_recovery_history(history: list[bool]) -> None:
    """Fixed operation counters match an independent success/failure history model."""
    from conftest import RecordingSink

    sink = RecordingSink()
    observer = DrainMetrics(Metrics(sink))
    faults = recoveries = 0
    faulted = False
    for index, failed in enumerate(history):
        if failed:
            faults += 1
            faulted = True
            with pytest.raises(OSError, match=r"^private$"):
                observer.call("terminate", lambda: _raise(OSError("private")))
        else:
            recoveries += int(faulted)
            faulted = False
            observer.call("terminate", lambda: None)
        record = sink.records[-1]
        assert int(record["BUILD_LIMITS_DRAIN_ATTEMPTS"]) == index + 1, (
            "each call is one attempt"
        )
        assert int(record["BUILD_LIMITS_DRAIN_FAILURES"]) == faults, (
            "only failed actions count"
        )
        assert int(record["BUILD_LIMITS_DRAIN_RECOVERIES"]) == recoveries, (
            "nonpending successes recover faults"
        )


def test_counter_saturation_and_operation_independence(
    metric_sink, monkeypatch
) -> None:
    """Repeated faults saturate independently and a new instance resets counters."""
    monkeypatch.setattr(drain_metrics, "MAX_COUNTER", 2)
    observer = DrainMetrics(Metrics(metric_sink))
    for _ in range(4):
        with pytest.raises(OSError, match=r"^private$"):
            observer.call("terminate", lambda: _raise(OSError("private")))
    observer.call("escalate", lambda: None)
    assert metric_sink.records[-2]["BUILD_LIMITS_DRAIN_ATTEMPTS"] == "2", (
        "attempts saturate"
    )
    assert metric_sink.records[-2]["BUILD_LIMITS_DRAIN_FAILURES"] == "2", (
        "faults saturate"
    )
    assert metric_sink.records[-1]["BUILD_LIMITS_DRAIN_ATTEMPTS"] == "1", (
        "operation and instance counts are independent"
    )
    assert metric_sink.records[-1]["BUILD_LIMITS_DRAIN_STATE"] == "completed", (
        "another operation has no inherited fault"
    )
    DrainMetrics(Metrics(metric_sink)).call("terminate", lambda: None)
    assert metric_sink.records[-1]["BUILD_LIMITS_DRAIN_ATTEMPTS"] == "1", (
        "operation and instance counts are independent"
    )
    assert metric_sink.records[-1]["BUILD_LIMITS_DRAIN_FAILURES"] == "0", (
        "new instance resets faults"
    )
