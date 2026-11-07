"""Stop a checker and the group it started.

The pinned checker is a launcher that starts a Go binary, so killing the
launcher alone leaves the work running. Everything here therefore acts on the
process group: the launcher is started in a new session, which makes its group
exactly the launcher and everything it spawned.

The two entry points answer the two ways a run ends early. :func:`kill_group`
is the escalation an interrupt needs, and :func:`stopped` is the one a timeout
needs: it kills the group and then collects what the checker printed first,
because that output is the only evidence of how far the run had got.
"""

import contextlib
import os
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - a fixed argv, never a shell.

__all__ = ["kill_group", "stopped"]

_KILL_GRACE_SECONDS = 5.0
"""How long a killed group has to exit after ``SIGTERM`` before ``SIGKILL``."""


def stopped(process: subprocess.Popen[bytes]) -> bytes:
    """Kill a run's group and collect what it printed before it went.

    Parameters
    ----------
    process : subprocess.Popen
        The launcher, whose output has not yet been read.

    Returns
    -------
    bytes
        The output the checker managed to print. It is the only evidence of
        how far the run had got, so it is kept rather than discarded: a report
        that said nothing beyond "it timed out" would hide the state count that
        says whether the model was still exploring, or stuck.

    Notes
    -----
    The second ``communicate`` blocks until every writer has closed its end of
    the pipe. Sending ``SIGKILL`` to the group is what guarantees that happens
    even when a grandchild inherited the pipe, so this call cannot hang on a
    daemon the launcher left behind.
    """
    kill_group(process)
    return process.communicate()[0]


def kill_group(process: subprocess.Popen[bytes]) -> None:
    """Ask a launcher's group to stop, then insist.

    Parameters
    ----------
    process : subprocess.Popen
        The launcher, started in its own session, so its process group is
        exactly the launcher and everything it started.

    Notes
    -----
    ``SIGTERM`` first gives the checker a chance to print why it was stopped,
    which is the difference between a report naming its cause and a report
    saying the run vanished. ``SIGKILL`` follows after the grace period, which
    is what handles a checker wedged inside the Go runtime.

    The escalation is unconditional and runs even when the launcher has already
    exited. ``process.wait()`` observes the launcher alone, so a child that
    ignores ``SIGTERM`` while the launcher obeys it would otherwise never
    receive ``SIGKILL``, and the following ``communicate`` would block on the
    pipe that child still holds. A group that has already gone is not an
    error: ``ProcessLookupError`` means the work is done, and the subsequent
    ``communicate`` collects whatever it printed.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
