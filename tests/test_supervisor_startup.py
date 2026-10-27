"""Tests for the supervisor's start-up barrier and recovery binding."""

import dataclasses as dc
import os
import signal
import socket
import subprocess
import typing as typ
from pathlib import PurePosixPath

import pytest
from descriptor_helpers import still_open
from pool_helpers import FakePool

from buildlimits import cgroups, startup
from buildlimits import supervisor as supervisor_module
from buildlimits.config import SupervisorConfig
from buildlimits.protocol import (
    RecoverReport,
    RecoverRequest,
    Refused,
    decode_reply,
    encode_request,
    recv_frame,
    send_frame,
)
from buildlimits.sccache import SccacheError
from buildlimits.supervisor import Supervisor
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

OWN_CGROUP = PurePosixPath(
    "/user.slice/user-1000.slice/user@1000.service/app.slice/s.service"
)


def _refusing_runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Answer every user-manager query with a failure."""
    return subprocess.CompletedProcess(argv, 1, "", "Connection timed out")


def test_an_unknown_server_load_keeps_the_pool_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed load query fails start-up before any token can be read."""
    monkeypatch.setattr(startup, "drain_slice", lambda *_args: 0)
    supervisor = Supervisor(
        SupervisorConfig(cache_mode="sccache"), UserManager(_refusing_runner)
    )
    with supervisor.selector:
        with pytest.raises(SccacheError, match="Connection timed out"):
            supervisor.start(OWN_CGROUP)
        assert supervisor.pool is None, "the token pool was never opened"
        assert supervisor.listener is None, "no request can be accepted"


@dc.dataclass(slots=True)
class _Started:
    """One started supervisor, the commands it issued, and its open pipes."""

    supervisor: Supervisor
    jobs: list[list[str]]
    pipes: list[int]

    def verbs(self) -> list[str]:
        """List the unit-control verbs the supervisor asked its manager for."""
        return [
            verb for argv in self.jobs for verb in ("stop", "start") if verb in argv
        ]

    def close(self) -> None:
        """Release the supervisor's descriptors, then the pipes this test made."""
        try:
            self.supervisor.close()
        finally:
            for fd in self.pipes:
                os.close(fd)


def _manager(
    argv: cabc.Sequence[str], jobs: list[list[str]]
) -> subprocess.CompletedProcess[str]:
    """Answer as a stopped, empty cache would, recording every command.

    A recording runner rather than a refusing one: the question is what the
    supervisor asked the manager for, so an unexpected command must appear as a
    recorded job rather than as a fault raised from inside the code under test.
    """
    jobs.append(list(argv))
    if "show" in argv:
        return subprocess.CompletedProcess(
            argv, 0, "MainPID=0\nControlGroup=\nActiveState=inactive\n", ""
        )
    return subprocess.CompletedProcess(argv, 0, "", "")


def _started(mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Started:
    """Start one supervisor in ``mode`` with every external boundary scripted.

    The slice drain, which would read ``/sys/fs/cgroup``, and the manager,
    whose runner would otherwise reach the real user manager, are scripted. The
    pool keeps a real pipe, because ``start`` registers its wake descriptor.
    """
    monkeypatch.setattr(startup, "drain_slice", lambda *_args: 0)
    monkeypatch.setattr(cgroups, "directory", lambda path: tmp_path / str(path)[1:])
    jobs: list[list[str]] = []
    pipes: list[int] = []

    def fake_pool(path: Path) -> FakePool:
        """Stand in for the shared device with a scripted pool."""
        del path
        read_end, write_end = os.pipe2(os.O_CLOEXEC)
        pipes.extend((read_end, write_end))
        return FakePool(wake_fd=read_end)

    monkeypatch.setattr(supervisor_module, "TokenPool", fake_pool)
    settings = SupervisorConfig(
        cache_mode=mode,
        # Keep the socket shallow: xdist's per-worker temporary directories
        # leave little of the 108-byte ``AF_UNIX`` path budget.
        socket_path=tmp_path / f"{mode}.sock",
        recovery_marker=tmp_path / mode / "cache-recovery",
    )
    supervisor = Supervisor(
        settings,
        UserManager(lambda argv: _manager(argv, jobs)),
        # The request and the ticks share this hand-driven clock, so each
        # tick's step is due rather than waiting on real time.
        clock=lambda: 0.0,
    )
    supervisor.start(OWN_CGROUP)
    return _Started(supervisor, jobs, pipes)


def _recover_over_the_wire(supervisor: Supervisor) -> object:
    """Send one recovery request down the socket the supervisor listens on.

    The listener is taken from the supervisor rather than built here, so the
    request reaches the binding :meth:`Supervisor.start` installed rather than
    the constructor default that precedes it. The test does not run
    :meth:`Supervisor.serve`, so the helper calls ``_accept`` itself to
    dispatch the connected request before reading the reply.

    Returns
    -------
    object
        The decoded reply, so a test asserts the supervisor's answer rather
        than the absence of one.
    """
    listener = supervisor.listener
    assert listener is not None, "the supervisor is listening after start-up"
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(listener.getsockname())
        client.settimeout(2)
        send_frame(client, encode_request(RecoverRequest()))
        supervisor._accept()
        frame = recv_frame(client)
        assert frame is not None, "a recovery request is always answered"
        return decode_reply(frame[0])
    finally:
        client.close()


def test_the_refusal_of_a_recovery_request_names_the_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a supervisor that manages a cache answers a recovery request.

    ``direct`` mode runs every compiler without a cache and requires the
    managed server to be stopped, so its recovery transaction has nothing to
    coordinate: the supervisor must refuse rather than stop and start a service
    it does not manage. ``kache`` mode is refused for the neighbouring reason:
    it caches, but its daemon is managed elsewhere, so there is still no unit
    for this supervisor to stop or start. An ``sccache`` supervisor over the
    same scripted manager is the control, proving the refusal comes from the
    mode rather than from the shape of the request.
    """
    for mode in ("direct", "kache", "sccache"):
        started = _started(mode, tmp_path, monkeypatch)
        try:
            marker = tmp_path / mode / "cache-recovery"
            supervisor = started.supervisor
            reply = _recover_over_the_wire(supervisor)
            if mode != "sccache":
                assert reply == Refused(
                    reason="no-cache-recovery",
                    message="this supervisor manages no sccache service",
                ), "the mode that owns no service refuses rather than acting"
            else:
                assert isinstance(reply, RecoverReport), (
                    "a mode with a managed cache does run the transaction"
                )
                assert reply.state["phase"] == "quiescing", (
                    "the reply carries the transaction the binding opened"
                )
            # Drive the transaction one step per tick. A refusal leaves it
            # idle, so it writes no barrier and never asks for a unit.
            supervisor.recovery.tick(0.0)
            assert marker.exists() is (mode == "sccache"), (
                "the barrier belongs to the mode that owns the cache"
            )
            for _tick in range(3):
                supervisor.recovery.tick(0.0)
            assert supervisor.recovery.describe(0.0)["phase"] == "idle", (
                "the transaction ran to its end in the mode that owns the cache"
            )
            assert marker.exists() is False, (
                "a completed transaction leaves the cache free to start"
            )
            assert started.verbs() == (
                ["stop", "start"] if mode == "sccache" else []
            ), "only the mode that owns the cache controls the unit"
        finally:
            started.close()


def test_closing_a_started_supervisor_releases_every_descriptor_it_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close returns the selector, listener, signal pipe and pool, and is repeatable.

    The signal wakeup descriptor and the ``SIGCHLD`` handler are process-wide, so
    a supervisor that left them installed would write into whichever unrelated
    descriptor later reuses the pipe's number.
    """
    started = _started("sccache", tmp_path, monkeypatch)
    supervisor = started.supervisor
    try:
        listener = supervisor.listener
        pool = typ.cast("FakePool", supervisor.pool)
        assert listener is not None, "start-up opened the requester listener"
        held = [
            supervisor.selector.fileno(),
            listener.fileno(),
            supervisor._signals_read,
            supervisor._signals_write,
        ]
        assert still_open(held) == held, "the started supervisor holds all four"

        supervisor.close()

        assert not still_open(held), "close returns every descriptor"
        assert pool.is_closed, "close releases the token pool"
        assert supervisor.pool is None, "a released pool is not left to be reused"
        assert signal.set_wakeup_fd(-1) == -1, (
            "the signal wakeup descriptor is withdrawn"
        )
        assert signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL, (
            "the SIGCHLD handler is withdrawn with it"
        )
        supervisor.close()
    finally:
        started.close()
