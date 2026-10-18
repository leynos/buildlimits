"""A supervisor restart is invisible to a build that was not yet admitted.

Before an ``Admitted`` reply no compiler has run, so a refusal as ``restarting``
and a connection that ends are both safe to answer by asking again. After it,
the compiler may have run, and losing the supervisor must still fail the build.
"""

import contextlib
import dataclasses as dc
import socket
import typing as typ

import pytest
from adapter_doubles import immediate_retry, script_adapter
from cache_unit_fake import FakeClock

from buildlimits import adapter
from buildlimits.adapter_invocation import (
    AdmissionAttempt,
    QueuedRestartError,
    retry_request,
)
from buildlimits.heartbeat import Heartbeat
from buildlimits.metrics import Metrics
from buildlimits.protocol import (
    Admitted,
    Exit,
    Finished,
    Queued,
    Refused,
    encode_reply,
)
from buildlimits.timing import RetryWait

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from conftest import RecordingSink

    from buildlimits.adapter_metrics import AdapterMetrics


RESTARTING = Refused("restarting", "the supervisor refused: restarting")
DONE = Exit(code=0)
ADMITTED_AND_DONE = [
    encode_reply(Admitted("g1", False)),
    encode_reply(Finished(DONE)),
]


def _restart_error(*, queued: bool) -> adapter.RestartingError:
    """Build the restart that a supervisor's reply or loss would raise.

    Returns
    -------
    RestartingError
        Marked queued when the request had been acknowledged first.
    """
    return (QueuedRestartError if queued else adapter.RestartingError)("restarting")


class _Summary:
    """Stands in for the metric summary, which only records an outcome."""

    outcome = "supervisor_lost"

    def __init__(self) -> None:
        """Start with no restart retried."""
        self.restart_retries = 0

    def retry_restart(self) -> None:
        """Count one retry after a restart, as the real summary does."""
        self.restart_retries += 1


def _script(
    owned: contextlib.ExitStack,
    monkeypatch: pytest.MonkeyPatch,
    scripts: list[list[bytes] | None],
) -> None:
    """Hand the adapter one connection per script, each pre-loaded with replies.

    A ``None`` script is a supervisor already gone: its peer is closed, so the
    adapter's first send meets a reset.
    """
    monkeypatch.setenv("BUILD_LIMITS_UNAVAILABLE_SECS", "30")
    pairs = [socket.socketpair() for _ in scripts]
    for (client, server), frames in zip(pairs, scripts, strict=True):
        owned.enter_context(client)
        owned.enter_context(server)
        for frame in frames or []:
            server.sendall(frame)
        if frames is None:
            server.close()
        else:
            server.shutdown(socket.SHUT_WR)
    script_adapter(monkeypatch, iter(client for client, _ in pairs), lambda exclude: [])


def _run() -> int:
    """Run one cacheable invocation without sleeping between attempts.

    Returns
    -------
    int
        The adapter's exit status.
    """
    return adapter.run(["rustc"], cacheable=True, retry=immediate_retry())


def test_a_restart_while_queued_is_retried_and_the_build_completes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Queued, then told to retry, then admitted by the next supervisor."""
    with contextlib.ExitStack() as owned:
        _script(
            owned,
            monkeypatch,
            [
                [encode_reply(Queued(1)), encode_reply(RESTARTING)],
                [
                    encode_reply(Queued(0)),
                    encode_reply(Admitted("g1", True)),
                    encode_reply(Finished(Exit(code=3))),
                ],
            ],
        )
        status = _run()
    assert status == 3, "the compiler's own status passes through after the restart"
    assert capsys.readouterr().err.count("the supervisor is restarting") == 1, (
        "the wait is explained to the user once"
    )


@pytest.mark.parametrize(
    "first",
    [[], [encode_reply(Queued(2))], [encode_reply(Queued(2))[:6]], None],
    ids=[
        "closed-before-any-reply",
        "closed-while-queued",
        "closed-inside-a-reply",
        "reset-while-sending",
    ],
)
def test_a_supervisor_lost_before_admission_is_retried(
    monkeypatch: pytest.MonkeyPatch, first: list[bytes] | None
) -> None:
    """A supervisor killed while the request waits is no reason to fail it."""
    with contextlib.ExitStack() as owned:
        _script(owned, monkeypatch, [first, ADMITTED_AND_DONE])
        assert _run() == 0, "the second supervisor ran the build"


def test_losing_the_supervisor_after_admission_still_fails_the_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compiler may have run, so the build is never resubmitted."""
    with contextlib.ExitStack() as owned:
        _script(
            owned,
            monkeypatch,
            [[encode_reply(Queued(0)), encode_reply(Admitted("g1", False))]],
        )
        with pytest.raises(adapter.AdapterError, match="closed the connection") as lost:
            _run()
    assert not isinstance(lost.value, adapter.RestartingError), (
        "an admitted request is not a restart"
    )


@dc.dataclass(slots=True)
class _Rig:
    """A hand-driven clock around one ``retry_request`` call."""

    first_deadline: float
    clock: FakeClock = dc.field(default_factory=FakeClock)
    deadlines: list[float] = dc.field(default_factory=list)

    def run(self, request: cabc.Callable[[], Exit]) -> Exit:
        """Retry ``request`` on this clock, whose sleep only advances it.

        Returns
        -------
        Exit
            The request's eventual result.
        """
        attempt = AdmissionAttempt(
            self.first_deadline, typ.cast("AdapterMetrics", _Summary())
        )
        timing = self.clock.timing()
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

    def request() -> Exit:
        """Refuse as restarting without ever having queued the request."""
        raise _restart_error(queued=False)

    with pytest.raises(adapter.RestartingError):
        rig.run(request)
    assert 5.0 <= rig.clock.time < 10.0, (
        "the loop ran to the deadline and stopped there"
    )


def test_a_restart_retry_is_counted_in_the_invocation_summary(
    monkeypatch: pytest.MonkeyPatch, metric_sink: RecordingSink
) -> None:
    """The adapter's one summary carries how often it had to ask again."""
    with contextlib.ExitStack() as owned:
        _script(
            owned,
            monkeypatch,
            [[encode_reply(RESTARTING)], [], ADMITTED_AND_DONE],
        )
        adapter.run(
            ["rustc"],
            cacheable=False,
            retry=immediate_retry(),
            metrics=Metrics(metric_sink, source="adapter"),
        )
    assert len(metric_sink.records) == 1, "one terminal summary per invocation"
    record = metric_sink.records[0]
    assert record["BUILD_LIMITS_RESTART_RETRIES"] == "2", (
        "a refusal and a lost connection each cost one fresh attempt"
    )
    assert record["BUILD_LIMITS_BUSY_RETRIES"] == "0", "restarts are not pressure"
