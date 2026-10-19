"""Turn ``SIGCHLD`` into an event-loop wakeup and account for ended leaders.

The event loop cannot wait on a child directly without blocking every other
descriptor, so the handler writes to a non-blocking self-pipe and the loop
drains it when the selector reports the read end ready. Reaping is deliberately
generous: every status the kernel offers is consumed before any fallible
diagnostic runs, so a failure cannot erase an exit that admission has not yet
been told about.
"""

import contextlib
import os
import selectors
import signal
import typing as typ

from buildlimits import group_recovery

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types

    from buildlimits.supervisor import Supervisor

SIGNALS_TAG = "signals"

type _Handler = cabc.Callable[[int, types.FrameType | None], object] | int | None


def watch_signals(supervisor: Supervisor) -> None:
    """Wake the event loop on ``SIGCHLD`` through a self-pipe.

    Parameters
    ----------
    supervisor : Supervisor
        Owner of the selector and the self-pipe's read descriptor.
    """
    read_end, write_end = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    supervisor._signals_read = read_end
    supervisor._signals_write = write_end
    previous_wakeup = signal.set_wakeup_fd(write_end, warn_on_full_buffer=False)
    # A handler must be installed for the wakeup descriptor to be written.
    previous_handler = signal.signal(signal.SIGCHLD, lambda *_: None)
    supervisor._signals_previous = (previous_wakeup, previous_handler)
    supervisor.selector.register(read_end, selectors.EVENT_READ, SIGNALS_TAG)


def release_signals(supervisor: Supervisor) -> None:
    """Stop waking the event loop and close both ends of the self-pipe.

    The wakeup descriptor and the ``SIGCHLD`` handler are process-wide, so the
    ones that :func:`watch_signals` displaced are restored before the pipe
    closes; otherwise a later signal would write to a descriptor number that
    something else may now own, and a host that owned them would lose them.
    Calling this again, or before :func:`watch_signals`, does nothing.

    Parameters
    ----------
    supervisor : Supervisor
        Owner of the self-pipe.
    """
    if supervisor._signals_read < 0:
        return
    previous_wakeup, previous_handler = supervisor._signals_previous
    # ``signal.signal`` returns None for a handler Python did not install.
    handler = typ.cast("_Handler", previous_handler)
    if handler is None:
        handler = signal.SIG_DFL
    try:
        signal.set_wakeup_fd(previous_wakeup)
        signal.signal(signal.SIGCHLD, handler)
    finally:
        _close_pipe(supervisor)


def _close_pipe(supervisor: Supervisor) -> None:
    """Close both ends of the self-pipe and mark it released."""
    for fd in (supervisor._signals_read, supervisor._signals_write):
        with contextlib.suppress(OSError):
            os.close(fd)
    supervisor._signals_read = -1
    supervisor._signals_write = -1


def reap_leaders(supervisor: Supervisor) -> None:
    """Drain the self-pipe, then collect every ended leader.

    Parameters
    ----------
    supervisor : Supervisor
        Owner of the self-pipe, the groups and the admission state.
    """
    with contextlib.suppress(BlockingIOError):
        while os.read(supervisor._signals_read, 4096):
            pass
    group_recovery.reap_leaders(
        supervisor.groups,
        supervisor.admission,
        supervisor._exec_outcome,
        supervisor._apply,
    )
