"""Retryable descriptor-pressure refusals preserve admission and bounded waits."""

import contextlib
import socket
import typing as typ

import pytest
from adapter_doubles import immediate_retry, script_adapter

from buildlimits import adapter
from buildlimits.protocol import (
    Admitted,
    Exit,
    Finished,
    Queued,
    Refused,
    encode_reply,
)
from buildlimits.timing import Clock, RetryWait

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def test_busy_retries_recapture_requests_and_preserve_final_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each busy refusal reconnects; only admitted completion ends the adapter."""
    with contextlib.ExitStack() as owned:
        pairs = [socket.socketpair() for _ in range(3)]
        for client, server in pairs:
            owned.enter_context(client)
            owned.enter_context(server)
        for _client, server in pairs[:2]:
            server.sendall(encode_reply(Refused("busy", "descriptor pressure")))
        pairs[2][1].sendall(encode_reply(Admitted("g1", False)))
        pairs[2][1].sendall(encode_reply(Finished(Exit(code=17))))
        connections = iter(client for client, _server in pairs)
        captured = []

        def capture(exclude: cabc.Collection[int]) -> list[int]:
            """Record the connection excluded by each fresh descriptor capture."""
            captured.append(tuple(exclude))
            return []

        script_adapter(monkeypatch, connections, capture)
        retry = immediate_retry()
        assert (
            adapter.run(
                ["rustc", "--crate-name", "pressure"], cacheable=False, retry=retry
            )
            == 17
        ), "busy retries preserve the admitted compiler exit status"
        assert len(captured) == 3, "each retry captures a fresh descriptor request"
        assert capsys.readouterr().err.count("waiting for descriptor headroom") == 1, (
            "repeated pressure produces one bounded waiting line"
        )


def test_busy_retry_exhaustion_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persistent pressure ends in an infrastructure refusal within its deadline."""
    clock = [0.0]
    attempts = []

    def refuse(*_args: object, **_kwargs: object) -> Exit:
        """Refuse every attempt before any compiler is admitted."""
        attempts.append(clock[0])
        msg = "pressure"
        raise adapter.BusyError(msg)

    def sleep(seconds: float) -> None:
        """Advance the clock without delaying the test runner."""
        assert seconds > 0, "retry backoff must make progress"
        clock[0] += seconds

    monkeypatch.setenv("BUILD_LIMITS_UNAVAILABLE_SECS", "0.2")
    monkeypatch.setattr(adapter, "_request_once", refuse)
    jitter_bounds: list[tuple[float, float]] = []

    def jitter(low: float, high: float) -> float:
        """Choose the largest permitted delay and record the policy bounds."""
        jitter_bounds.append((low, high))
        return high

    retry = RetryWait(clock=Clock(now=lambda: clock[0], sleep=sleep), jitter=jitter)
    with pytest.raises(adapter.AdapterError, match="retry deadline expired"):
        adapter.run(["rustc"], cacheable=False, retry=retry)
    assert jitter_bounds and all(bounds == (0.5, 1.0) for bounds in jitter_bounds), (
        "every retry uses the injected bounded jitter dependency"
    )
    assert clock[0] == pytest.approx(0.2), "backoff never exceeds the existing deadline"
    assert 2 <= len(attempts) <= 8, "bounded exponential retries avoid a busy loop"


@pytest.mark.parametrize("busy", [False, True])
def test_real_request_deadline_preserves_prior_refusal(
    monkeypatch: pytest.MonkeyPatch, *, busy: bool
) -> None:
    """Real socket attempts retain busy context without changing initial expiry."""
    clock = [0.0]
    calls = []
    monkeypatch.setenv("BUILD_LIMITS_UNAVAILABLE_SECS", "0.2")

    def sleep(_seconds: float) -> None:
        """Land the retry backoff exactly on its existing deadline."""
        clock[0] = 0.2

    retry = RetryWait(
        clock=Clock(now=lambda: clock[0], sleep=sleep), jitter=lambda _low, high: high
    )
    with contextlib.ExitStack() as owned:
        pairs = [socket.socketpair() for _ in range(2)]
        for client, server in pairs:
            owned.enter_context(client)
            owned.enter_context(server)
        pairs[0][1].sendall(encode_reply(Refused("busy", "descriptor table full")))
        connections = iter(client for client, _server in pairs)

        def connect(*_args: object) -> socket.socket:
            """Permit a real retry connection even when its deadline has elapsed."""
            calls.append(clock[0])
            return next(connections)

        def capture(**_kwargs: object) -> list[int]:
            """Exhaust the initial deadline only when no busy reply is expected."""
            if not busy:
                clock[0] = 0.2
            return []

        monkeypatch.setattr(adapter, "connect", connect)
        monkeypatch.setattr(adapter.descriptors, "inheritable_descriptors", capture)
        message = "retry deadline expired" if busy else "request deadline expired"
        with pytest.raises(adapter.AdapterError, match=message) as caught:
            adapter.run(["rustc"], cacheable=False, retry=retry)
        if busy:
            assert "last refusal: descriptor table full" in str(caught.value), (
                "deadline exhaustion must retain the preceding pressure refusal"
            )
        assert len(calls) == 1, "an expired busy retry cannot send a second request"


@pytest.mark.parametrize("recorded", [False, True])
def test_busy_after_recording_never_retries(
    monkeypatch: pytest.MonkeyPatch, *, recorded: bool
) -> None:
    """A queued or admitted request cannot be duplicated by a late busy reply."""
    client, server = socket.socketpair()
    with client, server:
        progress = Admitted("g1", False) if recorded else Queued(1)
        server.sendall(encode_reply(progress))
        server.sendall(encode_reply(Refused("busy", "late refusal")))
        calls = []

        def connect(*_args: object) -> socket.socket:
            """Return the one recorded connection and count every attempt."""
            calls.append(1)
            return client

        monkeypatch.setattr(adapter, "connect", connect)
        monkeypatch.setattr(
            adapter.descriptors, "inheritable_descriptors", lambda **_kwargs: []
        )
        with pytest.raises(adapter.AdapterError, match="late refusal") as caught:
            adapter.run(["rustc"], cacheable=False)
        assert not isinstance(caught.value, adapter.BusyError), (
            "a recorded refusal is final"
        )
        assert len(calls) == 1, "no duplicate admission request can be sent"
