"""Tests for the adapter's scope detection, rustup lookup and connection wait.

The request/reply exchange, ``_main`` and the two entry points live in
``test_adapter_run.py`` to keep each file under the house line-count limit.
"""

import errno
import signal
import typing as typ
from pathlib import Path, PurePosixPath

import pytest

from buildlimits import adapter
from buildlimits.adapter import AdapterError
from buildlimits.adapter_signals import cancel_on_signal
from buildlimits.heartbeat import Heartbeat
from buildlimits.protocol import Exit

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import socket


# --- in_admitted_scope ------------------------------------------------------


_ADMITTED = "/user.slice/buildlimits-compilers.slice/buildlimits-1-g1.scope"


@pytest.mark.parametrize(
    ("path", "env", "expected"),
    [
        (_ADMITTED, {}, True),
        ("/user.slice/other.scope", {}, False),
        (None, {}, False),
        (
            "/user.slice/custom.slice/buildlimits-1-g1.scope",
            {"BUILD_LIMITS_COMPILERS_SLICE": "custom.slice"},
            True,
        ),
    ],
    ids=["inside-slice", "outside-slice", "unknown-cgroup", "custom-slice"],
)
def test_in_admitted_scope(
    monkeypatch: pytest.MonkeyPatch,
    path: str | None,
    env: dict[str, str],
    *,
    expected: bool,
) -> None:
    """Only a scope under the configured compilers slice counts as admitted."""
    own = None if path is None else PurePosixPath(path)
    monkeypatch.setattr(adapter.cgroups, "own_path", lambda: own)
    assert adapter.in_admitted_scope(env) is expected, "admission follows the slice"


# --- connect -----------------------------------------------------------------


class _ScriptedSocket:
    """A fake ``socket.socket`` whose ``connect`` follows a scripted outcome."""

    def __init__(self, outcome: Exception | None) -> None:
        """Remember whether this instance's connect attempt should fail."""
        self._outcome = outcome
        self.closed = False

    def connect(self, _path: str) -> None:
        """Raise the scripted outcome, or succeed silently."""
        if self._outcome is not None:
            raise self._outcome

    def settimeout(self, _timeout: float | None) -> None:
        """Accept the bounded attempt timeout without real socket operations."""

    def close(self) -> None:
        """Record that the socket was closed."""
        self.closed = True


def test_connect_times_out_when_the_supervisor_never_appears() -> None:
    """A supervisor that is never reachable fails with a build-limits error."""
    attempts: list[_ScriptedSocket] = []

    def refuse() -> _ScriptedSocket:
        """Create a socket whose connection is refused."""
        attempts.append(_ScriptedSocket(ConnectionRefusedError("refused")))
        return attempts[-1]

    dialler = adapter.Dialler(
        make_socket=typ.cast("cabc.Callable[[], socket.socket]", refuse),
        sleep=lambda _seconds: None,
    )
    heartbeat = Heartbeat("demo", 45.0, clock=lambda: 0.0)
    with pytest.raises(AdapterError, match="build-limits supervisor is unavailable"):
        adapter.connect(Path("/no/such/socket"), -1.0, heartbeat, dialler)
    assert all(attempt.closed for attempt in attempts), "failed sockets are closed"


def test_connect_retries_then_succeeds() -> None:
    """A supervisor that becomes reachable on a later attempt is returned."""
    attempts = {"count": 0}
    waits: list[float] = []
    counted: list[bool] = []

    def make_socket() -> _ScriptedSocket:
        """Fail the first attempt and succeed on the second."""
        attempts["count"] += 1
        outcome = OSError("not yet") if attempts["count"] == 1 else None
        return _ScriptedSocket(outcome)

    dialler = adapter.Dialler(
        make_socket=typ.cast("cabc.Callable[[], socket.socket]", make_socket),
        sleep=waits.append,
        attempted=lambda: counted.append(True),
    )
    heartbeat = Heartbeat("demo", 45.0, clock=lambda: 0.0)
    result = adapter.connect(Path("/no/such/socket"), 100.0, heartbeat, dialler)
    assert len(counted) == 2, "each connection attempt is counted"
    assert attempts["count"] == 2, "connect was retried once before succeeding"
    assert len(waits) == 1, "the dialler waited once between attempts"
    assert isinstance(result, _ScriptedSocket), "the successful socket is returned"
    assert result.closed is False, "the returned socket is left open"


def test_connect_propagates_attempt_observer_error_before_socket_creation() -> None:
    """Observer errors propagate unchanged without allocating or retrying sockets."""
    events: list[str] = []
    failure = OSError("attempt observer unavailable")

    def make_socket() -> _ScriptedSocket:
        """Record any unexpected allocation of a connection attempt."""
        events.append("socket")
        return _ScriptedSocket(None)

    def observe() -> None:
        """Fail before any connection resources become owned."""
        raise failure

    dialler = adapter.Dialler(
        make_socket=typ.cast("cabc.Callable[[], socket.socket]", make_socket),
        sleep=lambda _seconds: events.append("sleep"),
        attempted=observe,
    )
    heartbeat = Heartbeat("demo", 45.0, clock=lambda: 0.0)
    with pytest.raises(OSError, match="attempt observer unavailable") as caught:
        adapter.connect(Path("/no/such/socket"), 100.0, heartbeat, dialler)
    assert caught.value is failure, "observer errors retain their identity"
    assert not events, "observer failure precedes socket creation and retry waiting"


def test_connect_calls_a_falsey_attempt_observer() -> None:
    """An available observer is invoked even if its truth value is false."""
    events: list[str] = []

    class FalseyObserver:
        """An attempt callback whose truth value must not determine availability."""

        def __bool__(self) -> bool:
            """Return false without making the callback absent."""
            return False

        def __call__(self) -> None:
            """Record the attempt before socket creation."""
            events.append("attempt")

    def make_socket() -> _ScriptedSocket:
        """Create a successful scripted socket after the observer runs."""
        events.append("socket")
        return _ScriptedSocket(None)

    dialler = adapter.Dialler(
        make_socket=typ.cast("cabc.Callable[[], socket.socket]", make_socket),
        attempted=FalseyObserver(),
    )
    heartbeat = Heartbeat("demo", 45.0, clock=lambda: 0.0)
    result = adapter.connect(Path("/no/such/socket"), 100.0, heartbeat, dialler)
    assert events == ["attempt", "socket"], "falsey observers run before allocation"
    assert isinstance(result, _ScriptedSocket), "the connected socket is returned"
    assert not result.closed, "the successful socket remains caller-owned"


# --- _reproduce ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("exit_", "expected"),
    [
        (Exit(code=0), 0),
        (Exit(code=17), 17),
        (Exit(code=None), adapter.FAILURE_STATUS),
    ],
)
def test_reproduce_passes_through_normal_exits(exit_: Exit, expected: int) -> None:
    """A normal or missing exit code maps directly to the adapter's own status."""
    assert adapter._reproduce(exit_) == expected, "the exit status is reproduced"


def test_reproduce_kills_itself_with_the_same_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signalled compiler is reproduced by signalling this process, not exec."""
    calls: list[tuple[str, object, object]] = []
    monkeypatch.setattr(
        adapter.signal,
        "signal",
        lambda number, handler: calls.append(("signal", number, handler)),
    )
    monkeypatch.setattr(
        adapter.os, "kill", lambda pid, number: calls.append(("kill", pid, number))
    )
    result = adapter._reproduce(Exit(signal=signal.SIGKILL))
    assert result == 128 + signal.SIGKILL, "the returned status reflects the signal"
    assert calls == [
        ("signal", signal.SIGKILL, signal.SIG_DFL),
        ("kill", adapter.os.getpid(), signal.SIGKILL),
    ], "the default disposition is restored before killing this process"


def test_reproduce_still_kills_when_the_disposition_cannot_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGKILL's fixed disposition does not stop the signal being reproduced."""
    killed: list[int] = []

    def refuse(number: int, _handler: object) -> typ.NoReturn:
        """Fail as the kernel does for a signal that cannot be caught.

        Raises
        ------
        OSError
            Always, as ``sigaction`` refuses SIGKILL and SIGSTOP.
        """
        raise OSError(errno.EINVAL, "Invalid argument", number)

    monkeypatch.setattr(adapter.signal, "signal", refuse)
    monkeypatch.setattr(adapter.os, "kill", lambda _pid, number: killed.append(number))
    result = adapter._reproduce(Exit(signal=signal.SIGKILL))
    assert killed == [signal.SIGKILL], "the signal is still sent to this process"
    assert result == 128 + signal.SIGKILL, "the fallback status reflects the signal"


# --- _cancel_on_signal -----------------------------------------------------------


def test_cancel_on_signal_registers_and_reproduces_every_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interruption by INT, TERM or HUP re-raises the same signal on exit."""
    registered: dict[int, cabc.Callable[[int, object], None]] = {}
    monkeypatch.setattr(
        adapter.signal,
        "signal",
        registered.__setitem__,
    )
    cancel_on_signal()
    assert set(registered) == {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}, (
        "every documented signal gets a handler"
    )
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(
        adapter.os, "kill", lambda pid, number: kills.append((pid, number))
    )
    registered[signal.SIGINT](signal.SIGINT, None)
    assert kills == [(adapter.os.getpid(), signal.SIGINT)], (
        "the handler kills this process with the caught signal"
    )
