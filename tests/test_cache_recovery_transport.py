"""Requester handling while the manager's control transport is stalled.

Recovery stops the managed cache by asking the user manager to queue a job. A
manager that accepts the connection and then stops answering must neither hold
the supervisor's event loop open nor be mistaken for a completed step. Both
halves are exercised here against the production ports: the transport bound is
the one :func:`buildlimits.systemd.run` applies, the private control and its
public-bus fallback are the real ones, and the requesters are real sockets.
The progress-and-cancellation test in :mod:`test_cache_recovery` calls
admission state directly, so it cannot see the socket that is still waiting for
its reply while the transaction stalls; this module covers exactly that.
"""

import contextlib
import socket
import subprocess
import typing as typ
from pathlib import Path

from cache_recovery_harness import Harness, harness_for, hold_drained_group
from cache_recovery_helpers import CacheState, runner_for
from request_helpers import compile_request

from buildlimits import systemd
from buildlimits.protocol import (
    Queued,
    RecoverReport,
    RecoverRequest,
    Reply,
    decode_reply,
    encode_request,
    recv_frame,
    send_frame,
)
from buildlimits.requesters import listen

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from buildlimits.supervisor import Supervisor

# The bound the transport applies to a control request, asserted as the literal
# the operator documentation states rather than recomputed from the production
# helper, so a change to that rule fails here instead of agreeing with itself.
_SHORT_BOUND = 3.0

# The verbs a control command names, in either transport's spelling: the private
# path passes ``stop``/``start``, and the public bus names the D-Bus method.
_CONTROL_VERBS = ("stop", "start")


def _is_control(argv: cabc.Sequence[str]) -> bool:
    """Report whether an argv asks the manager for a control verb.

    Parameters
    ----------
    argv : Sequence[str]
        The command line a transport would run.

    Returns
    -------
    bool
        Whether the command queues a stop or a start job.
    """
    return any(verb in argv or f"{verb.title()}Unit" in argv for verb in _CONTROL_VERBS)


class StalledTransport:
    """A manager transport that answers property queries and stalls controls.

    It is installed where :func:`~buildlimits.systemd.run` calls
    ``subprocess.run``, so the bound recorded on the way in is the production
    transport bound rather than one this double chose. Raising
    ``TimeoutExpired`` stands for a manager that accepts the connection and then
    stops answering, which is the transport fault the bound exists for.

    Parameters
    ----------
    state : CacheState
        The scripted cache whose property queries the transport still answers.
    """

    def __init__(self, state: CacheState) -> None:
        """Answer queries from the scripted cache and stall every control."""
        self.state = state
        self.queries = runner_for(state)
        self.bounds: list[float] = []

    def __call__(
        self, argv: cabc.Sequence[str], /, **options: object
    ) -> subprocess.CompletedProcess[str]:
        """Stall a control command, or answer a property query as scripted.

        Returns
        -------
        subprocess.CompletedProcess[str]
            The scripted manager answer, for queries only.

        Raises
        ------
        subprocess.TimeoutExpired
            For every control command: this transport never answers one.
        """
        bound = typ.cast("float", options["timeout"])
        if _is_control(argv):
            self.state.calls.append(list(argv))
            self.bounds.append(bound)
            raise subprocess.TimeoutExpired(list(argv), bound)
        return self.queries(argv)


def _stalled_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Harness, StalledTransport]:
    """Build a manual-policy supervisor whose manager transport stalls.

    The policy is manual so the only transaction in play is the one the test
    opens; the automatic trigger has its own module.

    Parameters
    ----------
    tmp_path : Path
        The disposable fixture directory.
    monkeypatch : pytest.MonkeyPatch
        Fixture used to install the stalled transport.

    Returns
    -------
    tuple[Harness, StalledTransport]
        The supervisor harness and the transport its manager now uses.
    """
    harness = harness_for(tmp_path, monkeypatch, mode="manual")
    transport = StalledTransport(harness.cache)
    monkeypatch.setattr(systemd.subprocess, "run", transport)
    # The recovery machine and the server watch share one manager port, so
    # routing that port through ``systemd.run`` is what applies the production
    # transport bound to the controls and queries under test.
    monkeypatch.setattr(harness.supervisor.manager, "runner", systemd.run)
    return harness, transport


def _serve(owned: contextlib.ExitStack, supervisor: Supervisor) -> Path:
    """Listen for real requesters on the supervisor's own socket path.

    The path is the fixture one the harness configured, never the operator's
    default: ``listen`` unlinks whatever already sits where it binds.

    Parameters
    ----------
    owned : contextlib.ExitStack
        Stack that closes the listener and the selector at the test's end.
    supervisor : Supervisor
        The supervisor whose requester path is under test.

    Returns
    -------
    Path
        The socket path a requester connects to.
    """
    owned.callback(supervisor.selector.close)
    path = Path(supervisor.settings.socket_path)
    supervisor.listener = owned.enter_context(listen(path))
    # The supervisor binds this port once it owns a managed cache; a test that
    # drives its own event loop has to bind it the same way.
    supervisor.requests.recovery = lambda: supervisor.recovery.request(
        "manual", supervisor.clock()
    )
    return path


def _connect(owned: contextlib.ExitStack, path: Path) -> socket.socket:
    """Open one real requester connection to the supervisor.

    Parameters
    ----------
    owned : contextlib.ExitStack
        Stack that closes the connection at the test's end.
    path : Path
        The supervisor's socket path.

    Returns
    -------
    socket.socket
        A connected client whose reads are bounded.
    """
    client = owned.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
    client.connect(str(path))
    client.settimeout(5)
    return client


def _submit(supervisor: Supervisor, client: socket.socket) -> tuple[int, Reply]:
    """Submit one compile request and read the progress reply it earns.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor whose ``_accept`` handles the connection.
    client : socket.socket
        The connected requester.

    Returns
    -------
    tuple[int, Reply]
        The recorded request identifier and the reply the client received.
    """
    send_frame(client, encode_request(compile_request()), [])
    before = set(supervisor.requests.pending)
    supervisor._accept()
    added = set(supervisor.requests.pending) - before
    assert len(added) == 1, "the connection is recorded under exactly one request"
    frame = recv_frame(client)
    assert frame is not None, "every recorded requester receives its progress reply"
    return added.pop(), decode_reply(frame[0])


def test_a_stalled_control_transport_faults_the_stop_within_the_short_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop that never completes is bounded, faulted, and never accounted.

    The manager accepts both connections and answers neither control, so the
    only ways out are the transport bound and the retry that follows it. A
    transaction that read the stall as progress would release a token and drop
    the barrier; this one must keep both.
    """
    harness, transport = _stalled_harness(tmp_path, monkeypatch)
    supervisor = harness.supervisor
    hold_drained_group(supervisor)
    supervisor.recovery.request("manual", harness.now)

    harness.tick()  # quiescing: the barrier is written before any control runs
    harness.tick()  # stopping: the private transport stalls

    document = harness.described()
    assert document["phase"] == "stopping", "a stalled stop is retried, never skipped"
    assert transport.bounds == [_SHORT_BOUND, _SHORT_BOUND], (
        "the private control and its public fallback both carry the short bound"
    )
    assert "cache.service" in str(document["last_fault"]), (
        "the remembered fault names the unit whose manager is unreachable"
    )
    assert harness.cache.verbs == ["stop", "stop"], (
        "the stop was attempted once on each transport"
    )
    assert harness.marker.exists(), "a stalled stop keeps the restart barrier"
    assert not harness.pool.returned, "a stalled stop releases no token"
    assert supervisor.admission.tokens_held() == 1, "the drained token stays held"


def test_requesters_keep_their_replies_and_cancellation_through_a_stalled_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Waiting requesters are still served, and still cancellable, over sockets.

    The transaction stalls on its control transport while one requester is
    already waiting. That socket must still be honoured: a second requester is
    admitted with its own progress reply, the first one's disconnect cancels
    exactly its request and renumbers the one that remains, and neither the
    stall nor the cancellation may move a token.
    """
    harness, transport = _stalled_harness(tmp_path, monkeypatch)
    supervisor = harness.supervisor
    hold_drained_group(supervisor)

    with contextlib.ExitStack() as owned:
        path = _serve(owned, supervisor)
        recovery = _connect(owned, path)
        send_frame(recovery, encode_request(RecoverRequest()), [])
        supervisor._accept()
        frame = recv_frame(recovery)
        assert frame is not None, "the recovery requester receives a report"
        report = decode_reply(frame[0])
        assert isinstance(report, RecoverReport), "the reply describes a transaction"
        assert report.state["phase"] == "quiescing", (
            "the requester's own socket opened the transaction"
        )

        harness.tick()  # quiescing: the barrier is written
        waiting = _connect(owned, path)
        first_id, first_reply = _submit(supervisor, waiting)
        assert first_reply == Queued(position=0), "the waiter is told its own place"

        harness.tick()  # stopping: the control transport stalls
        assert harness.described()["phase"] == "stopping", (
            "the stop faulted, not the queue"
        )
        assert transport.bounds == [_SHORT_BOUND, _SHORT_BOUND], (
            "the stalled control was bounded on both transports"
        )

        later = _connect(owned, path)
        second_id, second_reply = _submit(supervisor, later)
        assert second_reply == Queued(position=1), (
            "a requester arriving while the transaction stalls is still served"
        )

        waiting.close()
        supervisor._connection_ready(first_id)
        assert first_id not in supervisor.requests.pending, (
            "the cancellation is honoured"
        )
        assert list(supervisor.admission.queue) == [second_id], (
            "only the cancelled request left the queue"
        )
        assert first_id not in supervisor.admission.cacheable, (
            "its admission entry went with it"
        )
        frame = recv_frame(later)
        assert frame is not None, "the remaining requester is told its new place"
        assert decode_reply(frame[0]) == Queued(position=0), "it moved up the queue"

        assert supervisor.requests.retained == 1, "only the surviving socket is owned"
        assert not harness.pool.returned, "a cancelled waiter never returns a token"
        assert supervisor.admission.tokens_held() == 1, (
            "the drained token is still held"
        )
        assert harness.described()["phase"] == "stopping", (
            "requester handling changed nothing about the stalled transaction"
        )
