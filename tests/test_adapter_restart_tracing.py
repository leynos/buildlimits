"""Retry spans describe restarts without changing adapter behaviour."""

import dataclasses as dc
import socket
import typing as typ
from pathlib import Path

import pytest
from cache_unit_fake import FakeClock
from journal_fixtures import journal_fields, journal_receiver

from buildlimits import adapter
from buildlimits.adapter_invocation import AdmissionAttempt, QueuedRestartError
from buildlimits.adapter_retry import retry_request
from buildlimits.heartbeat import Heartbeat
from buildlimits.journal_native import MAX_DATAGRAM_BYTES, JournalSink
from buildlimits.protocol import Admitted, Exit, Finished, encode_reply
from buildlimits.timing import Clock, RetryWait
from buildlimits.tracing import JournalTracer

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.adapter_metrics import AdapterMetrics


DONE = Exit(code=0)


def _restart_error(*, queued: bool) -> adapter.RestartingError:
    """Build the restart that a supervisor's reply or loss would raise.

    Returns
    -------
    RestartingError
        Marked queued when the request had been acknowledged first.
    """
    return (QueuedRestartError if queued else adapter.RestartingError)("restarting")


def _always_restart(
    error: adapter.RestartingError,
) -> cabc.Callable[[], Exit]:
    """Return a request that repeatedly encounters the supplied restart."""

    def request() -> Exit:
        """Raise the same restart before the request can be admitted."""
        raise error

    return request


def _restart_once(
    error: adapter.RestartingError,
) -> cabc.Callable[[], Exit]:
    """Return a request that restarts once and then completes."""
    has_restarted = False

    def request() -> Exit:
        """Raise once, then return the completed compiler result."""
        nonlocal has_restarted
        if not has_restarted:
            has_restarted = True
            raise error
        return DONE

    return request


def _interrupt_wait(_seconds: float) -> None:
    """Interrupt a deterministic backoff wait."""
    raise KeyboardInterrupt


class _Summary:
    """Stand in for the metric summary, which only records restart counts."""

    outcome = "supervisor_lost"

    def __init__(self, trace_sink: JournalSink | None = None) -> None:
        """Start with no retries and an optional trace transport."""
        self.restart_retries = 0
        self.trace_sink = trace_sink

    def retry_restart(self) -> None:
        """Count one retry after a restart, as the real summary does."""
        self.restart_retries += 1

    def admission(self, _phase_started: float) -> None:
        """Accept the adapter's admission observation without metric effects."""


@dc.dataclass(slots=True)
class _Rig:
    """A hand-driven clock around one ``retry_request`` call."""

    first_deadline: float
    clock: FakeClock = dc.field(default_factory=FakeClock)
    deadlines: list[float] = dc.field(default_factory=list)

    def run(
        self,
        request: cabc.Callable[[], Exit],
        *,
        tracer: JournalTracer | None = None,
        sleep: cabc.Callable[[float], None] | None = None,
    ) -> Exit:
        """Retry ``request`` on this clock, whose sleep only advances it.

        Returns
        -------
        Exit
            The request's eventual result.
        """
        attempt = AdmissionAttempt(
            self.first_deadline,
            typ.cast("AdapterMetrics", _Summary()),
            tracer=tracer,
        )
        timing = self.clock.timing()
        if sleep is not None:
            timing = Clock(now=timing.now, sleep=sleep)
        retry = RetryWait(clock=timing, jitter=lambda _low, high: high)
        heartbeat = Heartbeat("crate", 600.0, clock=timing.now)

        def observed() -> Exit:
            """Record the deadline each attempt starts under, then delegate."""
            self.deadlines.append(attempt.deadline)
            return request()

        return retry_request(observed, heartbeat, attempt, retry)


def test_a_wait_in_the_queue_does_not_spend_the_restart_window() -> None:
    """After a long queued wait, the next supervisor still gets a full window."""
    rig = _Rig(first_deadline=300.0)

    def request() -> Exit:
        """Queue for ten minutes, be told to retry, then finish."""
        if len(rig.deadlines) == 1:
            rig.clock.advance(600.0)
            raise _restart_error(queued=True)
        return DONE

    assert rig.run(request) == DONE, (
        "the build finishes once the next supervisor admits it"
    )
    assert rig.deadlines[1] > 600.0 + 290.0, "the window restarted after the wait"


def test_a_supervisor_that_only_refuses_stays_bounded_by_the_first_deadline() -> None:
    """Unqueued restarting refusals never extend the original deadline."""
    rig = _Rig(first_deadline=5.0)

    with pytest.raises(adapter.RestartingError):
        rig.run(_always_restart(_restart_error(queued=False)))
    assert 5.0 <= rig.clock.time < 10.0, (
        "the loop ran to the deadline and stopped there"
    )


def test_restart_span_ends_at_admitted_before_finished_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An admitted reply ends retry tracing before the compiler finishes."""
    rig = _Rig(first_deadline=30.0)
    path = tmp_path / "journal.sock"
    sink = JournalSink(path)
    tracer = JournalTracer("adapter", sink)
    attempt = AdmissionAttempt(
        30.0,
        typ.cast("AdapterMetrics", _Summary()),
        tracer=tracer,
    )
    timing = rig.clock.timing()
    heartbeat = Heartbeat("crate", 30.0, clock=timing.now)
    retry = RetryWait(clock=timing, jitter=lambda _low, high: high)
    restart = adapter.RestartingError(
        "raw reason stays out of telemetry",
        failure_category="restart_refusal",
        stop_mode="graceful",
    )

    records: list[dict[str, str]] = []
    with journal_receiver(path) as receiver:
        client, supervisor = socket.socketpair()
        with client, supervisor:
            has_restarted = False
            original_admitted = heartbeat.admitted

            def request() -> Exit:
                """Restart once, then hold the compiler result until admitted."""
                nonlocal has_restarted
                if not has_restarted:
                    has_restarted = True
                    raise restart
                supervisor.sendall(encode_reply(Admitted("g1", False)))
                return adapter._await_result(client, heartbeat, attempt)

            def deliver_finished_after_observing_admission() -> None:
                """Hold the result until the retry span is already exported."""
                records.append(journal_fields(receiver.recv(MAX_DATAGRAM_BYTES)))
                assert records[0]["BUILD_LIMITS_TRACE_OUTCOME"] == "admitted", (
                    "the retry span has ended before the result is sent"
                )
                original_admitted()
                supervisor.sendall(encode_reply(Finished(DONE)))

            monkeypatch.setattr(
                heartbeat, "admitted", deliver_finished_after_observing_admission
            )
            try:
                assert retry_request(request, heartbeat, attempt, retry) == DONE, (
                    "the adapter receives the result only after admission"
                )
                receiver.settimeout(0.01)
                with pytest.raises(socket.timeout):
                    receiver.recv(MAX_DATAGRAM_BYTES)
            finally:
                sink.close()
    assert len(records) == 1, "the retry episode emits one completed span"
    record = records[0]
    assert record["BUILD_LIMITS_TRACE_OPERATION"] == "adapter_restart_retry", (
        "the record names the traced retry episode"
    )
    assert record["BUILD_LIMITS_TRACE_OUTCOME"] == "admitted", (
        "admission ends the retry span"
    )
    assert record["BUILD_LIMITS_TRACE_FAILURE_CATEGORY"] == "restart_refusal", (
        "the span preserves the bounded restart category"
    )
    assert record["BUILD_LIMITS_TRACE_STOP_MODE"] == "graceful", (
        "the span records the known stop mode"
    )
    assert record["BUILD_LIMITS_TRACE_RETRY_ATTEMPT"] == "1", (
        "the span counts its retry"
    )


@pytest.mark.parametrize(
    ("terminal_state", "expected_outcome"),
    [("deadline", "retry_exhausted"), ("interruption", "cancelled")],
    ids=["deadline", "interruption"],
)
def test_restart_span_closes_on_terminal_backoff(
    tmp_path: Path, terminal_state: str, expected_outcome: str
) -> None:
    """Deadline exhaustion and cancellation both end the retry span."""
    path = tmp_path / "journal.sock"
    sink = JournalSink(path)
    tracer = JournalTracer("adapter", sink)
    deadline = 0.0 if terminal_state == "deadline" else 30.0
    rig = _Rig(first_deadline=deadline)
    request = _always_restart(_restart_error(queued=False))

    with journal_receiver(path) as receiver:
        if terminal_state == "deadline":
            with pytest.raises(adapter.RestartingError):
                rig.run(request, tracer=tracer)
        else:
            with pytest.raises(KeyboardInterrupt):
                rig.run(request, tracer=tracer, sleep=_interrupt_wait)
        record = journal_fields(receiver.recv(MAX_DATAGRAM_BYTES))
    sink.close()
    assert record["BUILD_LIMITS_TRACE_OUTCOME"] == expected_outcome, (
        "the terminal outcome is recorded"
    )


def test_a_broken_trace_transport_cannot_prevent_an_admission_retry(
    tmp_path: Path,
) -> None:
    """Telemetry export failures remain outside compiler behaviour."""
    path = tmp_path / "journal-file"
    path.touch()
    sink = JournalSink(path)
    rig = _Rig(first_deadline=30.0)
    request = _restart_once(_restart_error(queued=False))

    assert rig.run(request, tracer=JournalTracer("adapter", sink)) == DONE, (
        "tracing failure does not stop the next admission attempt"
    )
    sink.close()
