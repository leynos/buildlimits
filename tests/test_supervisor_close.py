"""``Supervisor.close`` releases request, child, listener and signal resources."""

import contextlib
import os
import socket
from pathlib import Path
from unittest import mock

import pytest
from descriptor_helpers import still_open
from pool_helpers import attach_pool
from request_helpers import compile_request

from buildlimits import supervisor_shutdown, supervisor_signals
from buildlimits.config import SupervisorConfig
from buildlimits.groups import Running
from buildlimits.launch import HeldChild
from buildlimits.supervisor import Supervisor


@pytest.fixture
def supervisor(owned: contextlib.ExitStack) -> Supervisor:
    """Provide a direct-mode supervisor that is closed after the test.

    Returns
    -------
    Supervisor
        Never started, so it owns only its selector until the test adds more.
    """
    return owned.enter_context(
        contextlib.closing(Supervisor(SupervisorConfig(cache_mode="direct")))
    )


def test_close_releases_a_pending_request_and_a_running_childs_pipes(
    supervisor: Supervisor, tmp_path: Path, owned: contextlib.ExitStack
) -> None:
    """Each cleanup branch is exercised: removing one leaves a descriptor open.

    The request owns its connection and a received descriptor; the running group
    owns the barrier and error pipes of its held child. Closing the supervisor
    must return all four, and a second close must not disturb a stranger that
    now owns one of the released numbers.
    """
    client, server = socket.socketpair()
    owned.enter_context(client)
    received, received_peer = os.pipe()
    owned.callback(os.close, received_peer)
    barrier_peer, barrier = os.pipe()
    owned.callback(os.close, barrier_peer)
    errors, errors_peer = os.pipe()
    owned.callback(os.close, errors_peer)
    entry = supervisor.requests._admit(server, compile_request((0,)), [received])
    assert entry is not None, "the request is accepted and recorded"
    child = HeldChild(0, barrier, errors)
    supervisor.groups.running["g1"] = Running("g1", entry.request_id, tmp_path, child)
    owned_fds = [server.fileno(), received, barrier, errors]
    assert still_open(owned_fds) == owned_fds, "the supervisor holds all four"

    supervisor.close()

    assert not still_open(owned_fds), "close returns the request and the child's pipes"
    assert not supervisor.requests.pending, "no request is left recorded"
    supervisor.close()


def test_one_failing_release_does_not_strand_the_rest(
    supervisor: Supervisor, owned: contextlib.ExitStack
) -> None:
    """A release that raises still lets the pool, listener and selector close."""
    pool = attach_pool(supervisor)
    listener = owned.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
    supervisor.listener = listener
    selector_fd = supervisor.selector.fileno()
    boom = mock.Mock(side_effect=ValueError("signal state is main-thread only"))
    with mock.patch.object(supervisor_signals, "release_signals", boom):
        with pytest.raises(ValueError, match="main-thread only"):
            supervisor_shutdown.close(supervisor)
    assert pool.is_closed, "the pool was still released"
    assert listener.fileno() == -1, "the listener was still closed"
    assert not still_open([selector_fd]), "the selector was still closed"
    assert supervisor.pool is None, "the released pool is not left reachable"
    assert supervisor.listener is None, "the closed listener is not left reachable"


def test_the_signal_state_a_host_owned_is_handed_back(
    supervisor: Supervisor,
) -> None:
    """A displaced wakeup descriptor and handler return, not the defaults."""
    import signal

    host_read, host_write = os.pipe2(os.O_NONBLOCK)
    seen: list[int] = []

    def host_handler(number: int, _frame: object) -> None:
        """Stand in for a host process's own SIGCHLD handler."""
        seen.append(number)

    signal.set_wakeup_fd(host_write, warn_on_full_buffer=False)
    previous = signal.signal(signal.SIGCHLD, host_handler)
    try:
        supervisor._watch_signals()
        supervisor.close()
        assert signal.getsignal(signal.SIGCHLD) is host_handler, (
            "the host's handler is restored"
        )
        assert signal.set_wakeup_fd(-1) == host_write, (
            "the host's wakeup descriptor is restored"
        )
    finally:
        signal.signal(signal.SIGCHLD, previous)
        signal.set_wakeup_fd(-1)
        os.close(host_read)
        os.close(host_write)


def test_a_failed_release_cannot_change_the_service_exit_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The status the service settled on survives a close that raises."""
    from buildlimits import service

    broken = mock.Mock(spec=Supervisor)
    broken.close.side_effect = OSError("boom")
    service._release(broken)
    assert "releasing the supervisor's descriptors failed: OSError" in (
        capsys.readouterr().err
    ), "the failure is logged with its category only"
