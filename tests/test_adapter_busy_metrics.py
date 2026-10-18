"""Busy admission retries remain distinct from physical connection attempts."""

import typing as typ

import pytest

from buildlimits import adapter
from buildlimits.adapter_metrics import AdapterMetrics
from buildlimits.metrics import MAX_COUNTER, Metrics
from buildlimits.protocol import Exit
from buildlimits.timing import Clock, RetryWait

if typ.TYPE_CHECKING:
    from conftest import RecordingSink


def test_busy_retries_count_only_fresh_request_attempts(
    monkeypatch: pytest.MonkeyPatch, metric_sink: RecordingSink
) -> None:
    """Two busy replies followed by completion yield two retries, not wait counts."""
    calls = []

    def request(*_args: object, **_kwargs: object) -> Exit:
        """Model two busy replies followed by one terminal compiler result.

        Returns
        -------
        Exit
            The final compiler status after transient refusals.

        Raises
        ------
        adapter.BusyError
            On the two initial unrecorded requests.
        """
        calls.append(1)
        if len(calls) < 3:
            msg = "private refusal path and request identifier"
            raise adapter.BusyError(msg)
        return Exit(code=17)

    monkeypatch.setattr(adapter, "_request_once", request)
    retry = RetryWait(clock=Clock(sleep=lambda _seconds: None))
    assert (
        adapter.run(
            ["rustc"],
            cacheable=False,
            retry=retry,
            metrics=Metrics(metric_sink, source="adapter"),
        )
        == 17
    ), "the compiler status is unchanged"
    assert len(calls) == 3, "initial request plus two actual retries"
    assert len(metric_sink.records) == 1, "one invocation summary across all retries"
    record = metric_sink.records[0]
    assert record["BUILD_LIMITS_BUSY_RETRIES"] == "2", "only fresh retry requests count"
    assert record["BUILD_LIMITS_CONNECTION_ATTEMPTS"] == "0", (
        "request retries do not invent physical socket attempts"
    )
    assert "private" not in str(record), "refusal details never become metric fields"


def test_busy_expiry_does_not_count_a_request_that_never_started(
    monkeypatch: pytest.MonkeyPatch, metric_sink: RecordingSink
) -> None:
    """A wait ending at the overall deadline records no nonexistent retry."""
    now = [0.0]
    calls = []

    def refuse(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Refuse the initial request without recording it at the supervisor."""
        calls.append(1)
        msg = "descriptor pressure"
        raise adapter.BusyError(msg)

    def sleep(_seconds: float) -> None:
        """Exhaust the invocation deadline during the retry wait."""
        now[0] = 1.0

    monkeypatch.setenv("BUILD_LIMITS_UNAVAILABLE_SECS", "1")
    monkeypatch.setattr(adapter, "_request_once", refuse)
    retry = RetryWait(clock=Clock(now=lambda: now[0], sleep=sleep))
    with pytest.raises(adapter.AdapterError, match="retry deadline expired") as caught:
        adapter.run(
            ["rustc"],
            cacheable=False,
            retry=retry,
            metrics=Metrics(metric_sink, source="adapter"),
        )
    assert isinstance(caught.value.__cause__, adapter.BusyError), (
        "deadline expiry retains the preceding refusal as its cause"
    )
    assert len(calls) == 1, "no retry request starts after the deadline"
    assert metric_sink.records[0]["BUILD_LIMITS_BUSY_RETRIES"] == "0", (
        "a refused request and its wait are not themselves retries"
    )


def test_busy_retry_counter_caps_independently_of_socket_attempts(
    metric_sink: RecordingSink,
) -> None:
    """The independent counters saturate without crossing into each other."""
    summary = AdapterMetrics(lambda: 1.0, 0.0, Metrics(metric_sink, source="adapter"))
    summary.busy_retries = MAX_COUNTER - 1
    summary.restart_retries = MAX_COUNTER - 1
    summary.retry_busy()
    summary.retry_busy()
    summary.retry_restart()
    summary.retry_restart()
    summary.attempted()
    summary.finish()
    record = metric_sink.records[0]
    assert record["BUILD_LIMITS_BUSY_RETRIES"] == str(MAX_COUNTER), "retry count caps"
    assert record["BUILD_LIMITS_RESTART_RETRIES"] == str(MAX_COUNTER), (
        "the restart retry count caps independently"
    )
    assert record["BUILD_LIMITS_CONNECTION_ATTEMPTS"] == "1", "socket count is separate"
