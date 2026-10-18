"""How the supervisor decides to stop, and how it ends once everything has.

A stop arrives one of two ways and is carried out the same way. A signal
handler records the intent and returns, because a handler runs between
bytecodes and must not drain anything; the event loop turns that record into an
orderly stop on its next tick, where the ordering guarantees hold. A stop the
process cannot serve — an unhealthy pool, a failed read — ends the supervisor
directly instead.

Both paths converge on the same rule: a supervisor stops admitting and drains
every compiler group before it may exit, and the exit status says which of the
two endings it reached. The loop asks after every tick, so a stop that was
requested while it was busy still takes effect on the tick that follows.
"""

import typing as typ

from buildlimits.effects import Exit
from buildlimits.journal import log
from buildlimits.metrics import exit_outcome

if typ.TYPE_CHECKING:
    from buildlimits.supervisor import Supervisor


def request_stop(supervisor: Supervisor) -> None:
    """Record signal intent for orderly shutdown on the next tick.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor the signal handler belongs to.
    """
    supervisor._stop_requested = True


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


def honour_stop_request(supervisor: Supervisor) -> None:
    """Begin the stop a signal handler asked for, on the event loop.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor whose pending stop request is honoured.
    """
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
