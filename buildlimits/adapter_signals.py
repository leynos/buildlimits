"""Adapter interruption handlers with invocation-local metric observation."""

import contextlib
import os
import signal
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.protocol import Exit

SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def cancel_on_signal(interrupted: cabc.Callable[[], None] | None = None) -> None:
    """Cancel by reproducing a signal after optional silent observation.

    Parameters
    ----------
    interrupted : Callable[[], None] | None
        Runs before reproducing the signal; metric errors are suppressed.

    Notes
    -----
    The compiler belongs to the supervisor. Exiting closes the adapter's
    connection, causing the supervisor to terminate that compiler's group.
    """

    def handler(number: int, _frame: object) -> None:
        """Observe interruption, then die with the original disposition."""
        if interrupted is not None:
            with contextlib.suppress(Exception):
                interrupted()
        signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)

    for number in SIGNALS:
        signal.signal(number, handler)


@contextlib.contextmanager
def observe_signals(
    interrupted: cabc.Callable[[], None],
) -> cabc.Generator[None]:
    """Install invocation-local cancellation handlers and restore them on return.

    Parameters
    ----------
    interrupted : Callable[[], None]
        Silent observer called before signal reproduction.

    Yields
    ------
    None
        The invocation with cancellation observation installed.
    """
    previous = {number: signal.getsignal(number) for number in SIGNALS}
    try:
        cancel_on_signal(interrupted)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _reproduce(exit_: Exit) -> int:
    """Turn the compiler's ending into this process's ending.

    Returns
    -------
    int
        The status to exit with, unless a signal ends the process first.
    """
    if exit_.signal is not None:
        # SIGKILL and SIGSTOP cannot have their disposition changed, so the
        # reset raises; their default action is already the one reproduced.
        with contextlib.suppress(OSError, ValueError):
            signal.signal(exit_.signal, signal.SIG_DFL)
        os.kill(os.getpid(), exit_.signal)
        return 128 + exit_.signal
    if exit_.code is None:
        return 1
    return exit_.code
