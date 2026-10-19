"""Composition decisions for requests admitted across a recovery transaction.

:mod:`test_cache_recovery` covers the transaction itself: what each phase does,
what a refused step leaves behind, and which tokens may return.
:mod:`test_admission_cache_permission` covers the admission decision given a
permission, driven by a list the test flips. This module covers the seam
between them: a real :class:`~buildlimits.supervisor.Supervisor` whose
admission carries its own watch's permission, driven through a real recovery
transaction. A scripted permission can show that admission reads the answer at
the launch; only the wired stack can show that the answer the recovery
withholds is the one admission reads, and that the transaction is what
withdrew it.

What a token then does is deliberately out of view. The launch decision is
settled inside :meth:`Admission.token_arrived`, which returns the effect and
records the group before anything is spawned, so these tests read the decision
where it is made rather than through a compiler that would need a real scope.
Composing a compiler with a server the transaction is about to stop is the
failure under test: the started server would come up inside the compiler's own
scope, where nothing accounts for it.

The three cases are the three intervals a waiting request can be admitted in —
while the server is stopped, after it has been restored, and while the
transaction is failing to restore it. Sibling modules keep both under the
module-size ceiling.
"""

import contextlib
import socket
import typing as typ
from pathlib import Path

from cache_recovery_harness import (
    Harness,
    HarnessFactory,
    hold_drained_group,
    queue_request,
)

from buildlimits.effects import Launch

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.supervisor import Supervisor

SOCKET_PATH = "sccache.sock"


def _listen(path: Path) -> socket.socket:
    """Bind a listener where the supervisor probes for the managed server.

    The probe connects to the configured socket and reads nothing, so a
    listener at that path is the whole of "the server is up": without one, no
    reading in this module could ever be the healthy one, and a denial would be
    indistinguishable from a socket path that never existed.

    Parameters
    ----------
    path : Path
        The managed server's socket path.

    Returns
    -------
    socket.socket
        The listening socket, owned by the caller.
    """
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(8)
    return listener


@contextlib.contextmanager
def _serving_server(path: Path) -> cabc.Iterator[None]:
    """Answer the reachability probe as a running server does.

    Parameters
    ----------
    path : Path
        The managed server's socket path.

    Yields
    ------
    None
        While the socket answers probes.
    """
    listener = _listen(path)
    try:
        yield
    finally:
        listener.close()
        path.unlink(missing_ok=True)


def _healthy(supervisor: Supervisor) -> None:
    """Take the healthy reading, and prove it is the healthy one.

    The loop takes this reading on its own cache-poll cadence; the harness pins
    that cadence out of the way so a test advances only the transaction, so the
    reading is taken here rather than by waiting five seconds for it. Reading
    and checking it together is what keeps every test below from resting on a
    denial that a socket path that never existed would produce just as well: a
    test that could not first reach the permitted reading could not tell the
    transaction's withdrawal from its own broken fixture.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor whose composition reading is refreshed and checked.
    """
    supervisor.server.refresh_cache_mode()
    assert supervisor.server.cache_allowed() is True, "the server is up"


def _admitted(supervisor: Supervisor) -> tuple[Launch, bool]:
    """Read the launch a token produces and the group's own record of it.

    The two are returned together because they must agree: a group that ran
    directly while it believed it was cached would retain its token for
    server-work accounting that nothing will ever deliver.

    Parameters
    ----------
    supervisor : Supervisor
        The started supervisor whose admission receives the token.

    Returns
    -------
    tuple[Launch, bool]
        The launch effect the admission decided, and ``cached`` as the group
        it just created recorded it.
    """
    effects = supervisor.admission.token_arrived(b"+")
    launch = effects[0]
    assert isinstance(launch, Launch), "the waiting request is still admitted"
    group = supervisor.admission.groups[launch.group_id]
    return launch, group.cached


def _waiting(
    recovery_harnesses: HarnessFactory, *, refuse: str | None = None
) -> Harness:
    """Hold one drained group with one request waiting behind it.

    The transaction is not opened here: each test first takes the healthy
    reading, so the withdrawal it observes is the transaction's own.

    Parameters
    ----------
    recovery_harnesses : HarnessFactory
        Builds the harness and closes it when the test ends.
    refuse : str or None
        The control verb the scripted manager refuses, or None for none.

    Returns
    -------
    Harness
        The harness, ready for the healthy reading and the manual request.
    """
    harness = recovery_harnesses.build()
    hold_drained_group(harness.supervisor)
    queue_request(harness.supervisor)
    harness.cache.refuse = refuse
    return harness


def _open(harness: Harness) -> None:
    """Take the healthy reading, then open the manual transaction.

    Parameters
    ----------
    harness : Harness
        The harness whose supervisor opens the transaction.
    """
    _healthy(harness.supervisor)
    harness.supervisor.recovery.request("manual", harness.now)


def _account_as_the_server_stops(
    harness: Harness, listener: socket.socket, path: Path
) -> None:
    """Write the barrier, stop the cache and account for it.

    The stop job closes the listening socket, so the server is down from the
    accepted stop onwards; only a start job reopens it.

    Parameters
    ----------
    harness : Harness
        The harness whose transaction is open.
    listener : socket.socket
        The managed server's listener, closed when the stop is accepted.
    path : Path
        The managed server's socket path, removed with the listener.
    """
    # The first tick writes the barrier; the second has the stop accepted.
    for _phase in ("quiescing", "stopping"):
        harness.tick()
    listener.close()
    path.unlink()
    harness.cache.active = False
    harness.tick()  # accounting: the verified stop returns the drained token


def test_a_token_arriving_mid_transaction_is_admitted_without_the_cache(
    tmp_path: Path, recovery_harnesses: HarnessFactory
) -> None:
    """A server between stop and restart never composes a group.

    The reading that permits composition was taken by a reachability probe
    while the server was healthy, and the cache poll that would revise it runs
    on a five-second cadence — so a request queued before the transaction and
    granted a token during it would otherwise be composed from a stale reading
    of a server the transaction has already told to stop.
    """
    harness = _waiting(recovery_harnesses)
    supervisor = harness.supervisor

    with _serving_server(tmp_path / SOCKET_PATH):
        _open(harness)
        harness.tick()
        harness.tick()
        assert harness.cache.verbs == ["stop"], "the stop is queued before the token"
        assert supervisor.server.cache_allowed() is False, "composition is withdrawn"

        launch, recorded = _admitted(supervisor)
        assert launch.cached is False, "a held cache composes no group"
        assert recorded is False, "the group's own record agrees with the launch"
        assert supervisor.admission.groups[launch.group_id].token == b"+", (
            "and the token is held by the running group"
        )


def test_a_request_waiting_through_the_whole_transaction_is_cached_again(
    tmp_path: Path, recovery_harnesses: HarnessFactory
) -> None:
    """The denial ends with the restart; the next token composes the cache.

    The counterpart of the test above, and the reason the hold is a deadline
    rather than a permanent state: a request that waited out the whole
    transaction must compose with the cache the transaction restored, or the
    recovery would have denied caching for ever.
    """
    harness = _waiting(recovery_harnesses)
    supervisor = harness.supervisor
    socket_path = tmp_path / SOCKET_PATH
    listener = _listen(socket_path)
    try:
        _open(harness)
        _account_as_the_server_stops(harness, listener, socket_path)
        # The restart is queued rather than awaited, so the server has to be
        # answering before the transaction can observe it back in service.
        listener = _listen(socket_path)
        harness.advance(31)
        harness.tick()  # restarting: the start is accepted and observed
        assert harness.cache.verbs == ["stop", "start"], "the restart was issued"
        assert harness.described()["phase"] == "idle", "the transaction has ended"
        assert supervisor.server.cache_allowed() is True, "composition is permitted"
        # The release is evidence-gated: the transaction re-probes the server
        # before it permits composition, so the reading that admits the group
        # below is one taken after the restart, never the pre-stop one.
        _healthy(supervisor)

        launch, recorded = _admitted(supervisor)
        assert launch.cached is True, "the restarted cache composes the group"
        assert recorded is True, "the group's own record agrees with the launch"
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)


def test_a_restart_that_is_only_accepted_composes_no_cached_group(
    tmp_path: Path, recovery_harnesses: HarnessFactory
) -> None:
    """An unobserved restart keeps the cache out of composition.

    The start job is queued, not awaited, so acceptance says nothing about
    whether the managed server is listening again. The composition flag still
    holds the reading taken before the stop, and the cache poll that would
    revise it runs on a five-second cadence — so a token granted in that window
    would otherwise be composed with a server that is still down. A compiler
    that cannot reach the server starts one of its own inside its own scope,
    where nothing accounts for it, which is the same failure the stop window is
    held against.
    """
    harness = _waiting(recovery_harnesses)
    supervisor = harness.supervisor
    socket_path = tmp_path / SOCKET_PATH
    listener = _listen(socket_path)
    try:
        _open(harness)
        # This test leaves the socket closed, so the restart has been accepted
        # and has not been observed.
        _account_as_the_server_stops(harness, listener, socket_path)
        harness.advance(31)
        harness.tick()  # restarting: the start is accepted, readiness is not

        assert harness.cache.verbs == ["stop", "start"], "the restart was issued"
        # The transaction still ends: the start was issued, its accounting is
        # complete and its barrier may be cleared. Ending it is not the defect
        # — permitting composition on the strength of the issue would be.
        assert harness.described()["phase"] == "idle", "the transaction has ended"
        # The denial is no longer a deadline: the hold has lapsed, and what
        # keeps the cache out of a group is the reading the release took. That
        # is the whole point — a deadline would have expired whatever the
        # server was doing.
        assert supervisor.server.cache_allowed() is True, "the hold has lapsed"
        assert supervisor.admission.cached is False, (
            "the release's own probe found no server"
        )

        launch, recorded = _admitted(supervisor)
        assert launch.cached is False, "an unobserved restart composes no group"
        assert recorded is False, "the group's own record agrees with the launch"
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)


def test_an_admission_mid_failure_keeps_the_cache_out_of_composition(
    tmp_path: Path, recovery_harnesses: HarnessFactory
) -> None:
    """A request admitted amid failing retries never composes the stopped cache.

    A refused start leaves the server stopped while the composition flag still
    holds the reading taken while it was healthy. Composing a compiler in that
    state starts a server of its own inside the compiler's own scope, where
    nothing can account for it; the request must be admitted without the cache
    instead.
    """
    harness = _waiting(recovery_harnesses, refuse="start")
    supervisor = harness.supervisor

    with _serving_server(tmp_path / SOCKET_PATH):
        _open(harness)
        harness.account()
        harness.advance(31)
        harness.tick()  # restarting: the refused start keeps the hold

        assert harness.described()["last_fault"], "the refusal is kept for the operator"
        assert supervisor.server.cache_allowed() is False, (
            "a refused restart leaves composition denied"
        )

        launch, recorded = _admitted(supervisor)
        assert launch.cached is False, "a stopped cache composes no group"
        assert recorded is False, "the group's own record agrees with the launch"


def test_a_group_admitted_without_the_cache_does_not_block_the_stop(
    tmp_path: Path, recovery_harnesses: HarnessFactory
) -> None:
    """The stop proceeds past a group the transaction composed without the cache.

    The barrier is written on one tick and the stop is queued on the next, and
    admission keeps granting tokens in between. A busy queue fills every free
    slot in that gap, and the transaction itself composes each such group
    without the cache, so stopping the cache cannot interrupt it. Counting it as
    moving work refused the stop on every retry while the retained tokens stayed
    held, which is the livelock this guards against.
    """
    harness = _waiting(recovery_harnesses)
    supervisor = harness.supervisor

    with _serving_server(tmp_path / SOCKET_PATH):
        _open(harness)
        harness.tick()  # quiescing: the barrier is written
        assert harness.described()["phase"] == "stopping", "the barrier is down"

        launch, recorded = _admitted(supervisor)
        assert (launch.cached, recorded) == (False, False), (
            "the transaction composes the waiting request without the cache"
        )

        harness.tick()  # stopping: the uncached group is still running
        assert harness.cache.verbs == ["stop"], (
            "an uncached running group does not hold the stop"
        )
        assert harness.described()["phase"] == "accounting", (
            "the transaction moves on to accounting"
        )
