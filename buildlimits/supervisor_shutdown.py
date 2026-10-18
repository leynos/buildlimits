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

import typing as typ

from buildlimits.admission_state import GroupState
from buildlimits.effects import Exit
from buildlimits.journal import log
from buildlimits.metrics import exit_outcome
from buildlimits.service_readiness import notify_status

if typ.TYPE_CHECKING:
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
    if supervisor._graceful_stop_requested:
        supervisor._graceful_stop_requested = False
        graceful_stop(supervisor)
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
