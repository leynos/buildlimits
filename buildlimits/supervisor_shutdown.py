"""How the supervisor decides to stop, and how it ends once everything has.

A stop is either graceful or immediate, and a signal handler only records which.
The handler runs between bytecodes and must not drain anything; the event loop
turns the record into an orderly stop on its next tick, where the ordering
guarantees hold. A stop the process cannot serve — an unhealthy pool, a failed
read — ends the supervisor directly instead.

``SIGTERM``, which is what ``systemctl stop`` and ``restart`` send, is the
graceful stop: running groups finish, nothing new launches, and what still
waits is told to retry. ``SIGINT`` is the immediate stop: the queue is refused
and every running group is terminated. Neither may exit before every compiler
group has drained, and the exit status says which ending the process reached.
The loop asks after every tick, so a stop that was requested while it was busy
still takes effect on the tick that follows.
"""

import contextlib
import typing as typ

from buildlimits import supervisor_signals
from buildlimits.admission_state import GroupState
from buildlimits.effects import Exit
from buildlimits.journal import log
from buildlimits.metrics import exit_outcome
from buildlimits.service_readiness import notify_status

if typ.TYPE_CHECKING:
    from buildlimits.pending_request import Pending
    from buildlimits.supervisor import Supervisor

TOKEN_SETTLE_POLLS = 30
TOKEN_SETTLE_SECONDS = 0.01
_STATUS = "Stopping: waiting for running compiler groups to finish"
_GRACEFUL_STOP = "stopping: finishing running compiler groups before exit"


def request_stop(supervisor: Supervisor) -> None:
    """Record signal intent for an immediate shutdown on the next tick.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor the signal handler belongs to.
    """
    supervisor._stop_requested = True


def request_graceful_stop(supervisor: Supervisor) -> None:
    """Record signal intent for a graceful shutdown on the next tick.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor the signal handler belongs to.
    """
    supervisor._graceful_stop_requested = True


def graceful_stop(supervisor: Supervisor) -> None:
    """Let running groups finish, launch nothing more, then exit.

    No token is requested from here on, no running group is terminated, and
    requests that wait or arrive stay queued until the last group has emptied;
    they are then refused as ``restarting``, which the adapter retries. An
    immediate stop that follows still takes effect, and a second graceful stop
    changes nothing.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor to wind down.
    """
    if supervisor.admission.closing or not supervisor.admission.healthy:
        return
    supervisor._stopping = True
    log(_GRACEFUL_STOP)
    notify_status(_STATUS)
    supervisor._metric("health_changed", health="restarting")
    supervisor._apply(supervisor.admission.begin_closing())


def honour_graceful_request(supervisor: Supervisor) -> None:
    """Begin the graceful stop a signal handler asked for, if one is pending.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor whose pending graceful request is honoured.
    """
    if supervisor._graceful_stop_requested:
        supervisor._graceful_stop_requested = False
        graceful_stop(supervisor)


def fail_placement(supervisor: Supervisor, entry: Pending, group_id: str) -> None:
    """Account for a group that could not be placed, retrying it if a stop did it.

    The signal handler only records a graceful stop, so a placement that the
    stop broke (``systemctl stop`` signals the ``busctl`` that creates the scope
    when the unit's kill mode is ``control-group``) is seen before admission is
    closing. Honouring the pending request first makes the refusal
    ``restarting``, which the adapter retries within its deadline, instead of a
    launch failure. The cause stays in the journal, where
    :meth:`Groups.place` logged it; the requester is not told of a launch error,
    because nothing ran. With no graceful stop, the failure is reported as it
    always was.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor whose request could not be placed.
    entry : Pending
        The request whose compiler group could not be placed.
    group_id : str
        The admission group that was never launched.
    """
    honour_graceful_request(supervisor)
    if supervisor.admission.winding_down:
        entry.exec_error = None
    supervisor._apply(supervisor.admission.launch_failed(group_id))


def settle_graceful_stop(supervisor: Supervisor) -> None:
    """Open the one recovery that can release the tokens a graceful stop awaits.

    A cached group that ended by signal keeps its token until the cache is
    verified stopped and empty, and nothing else stops the cache when no
    request waits. Once only such groups remain, the supervisor opens the
    recovery transaction itself; it is idempotent while one runs, and it never
    releases a token, which still waits for the strict accounting.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor whose graceful stop is in progress.
    """
    admission = supervisor.admission
    states = {group.state for group in admission.groups.values()}
    if admission.winding_down and states == {GroupState.SERVER_DRAIN}:
        supervisor.recovery.request("manual", supervisor.clock())


def stop(supervisor: Supervisor) -> None:
    """Begin an orderly shutdown, draining every group first.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor to drain.
    """
    supervisor._stopping = True
    if supervisor.admission.healthy:
        log("stopping: draining every compiler group before exit")
        effects = supervisor.admission.health_failed()
        supervisor._metric("health_changed", health="stopping")
        supervisor._apply(effects)


def settle_token_request(supervisor: Supervisor) -> None:
    """Hand back a token the pool reader took for a request nobody will launch.

    A request that waited when the stop began leaves the pool's reader thread
    blocked on the device. The first token that returns satisfies it, and that
    token must go back before the process exits: guildmaster returns it when
    the handle closes, but a pool that does not account for its readers would
    lose it. The wait is bounded because an empty pool never satisfies the
    reader, and then the device's own accounting is all that remains.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor that has just finished draining.
    """
    for _ in range(TOKEN_SETTLE_POLLS):
        if not supervisor.admission.token_requested:
            return
        supervisor._pool_events()
        supervisor.timing.sleep(TOKEN_SETTLE_SECONDS)


def honour_stop_request(supervisor: Supervisor) -> None:
    """Begin the stop a signal handler asked for, on the event loop.

    The graceful request is honoured first, so that a graceful and an
    immediate request that arrive together end as an immediate stop. The loop
    calls this before it dispatches each ready descriptor as well as on every
    tick, so a token that arrives in the same cycle as the signal is returned
    instead of launching a compiler the stop has already ruled out.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor whose pending stop request is honoured.
    """
    honour_graceful_request(supervisor)
    if supervisor._stop_requested:
        supervisor._stop_requested = False
        stop(supervisor)


def shutdown(supervisor: Supervisor) -> None:
    """Stop the event loop once everything has drained.

    A failed drain restarts through the barrier rather than exiting, so the
    status distinguishes an orderly stop from one the process could not serve.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor whose exit status is settled here.
    """
    # Failed drains restart through the barrier after accounting completes.
    if supervisor.exit_status is not None:
        return
    orderly = supervisor._stopping and supervisor.health_reason is None
    supervisor.exit_status = 0 if orderly and not supervisor.loop.failures else 1

    supervisor._metric(
        "supervisor_stopped", outcome=exit_outcome(Exit(code=supervisor.exit_status))
    )


def close(supervisor: Supervisor) -> None:
    """Release every descriptor the supervisor owns; calling again is harmless.

    A stopped supervisor leaves its selector, listener, token pool, signal
    self-pipe and request connections open for process exit to reap. A process
    that outlives the supervisor, such as a test runner, must close them, so
    this is the one place that does. It does not stop or drain: call it after
    :meth:`~buildlimits.supervisor.Supervisor.serve` returns, or on a
    supervisor that never started.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor whose descriptors are released.

    Examples
    --------
    >>> from buildlimits.config import SupervisorConfig
    >>> from buildlimits.supervisor import Supervisor
    >>> supervisor = Supervisor(SupervisorConfig())
    >>> supervisor.close()
    >>> supervisor.close()
    """
    listener, supervisor.listener = supervisor.listener, None
    pool, supervisor.pool = supervisor.pool, None
    # Callbacks run last-in first-out, and each runs even if an earlier one
    # raised, so one failing release cannot strand the descriptors after it.
    # The first error still propagates once every release has been tried.
    with contextlib.ExitStack() as release:
        release.callback(supervisor.selector.close)
        if pool is not None:
            release.callback(pool.close)
        if listener is not None:
            release.callback(listener.close)
        release.callback(supervisor_signals.release_signals, supervisor)
        for running in supervisor.groups.running.values():
            release.callback(running.child.close)
        release.callback(supervisor.requests.close_all)
