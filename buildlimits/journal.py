"""The supervisor's diagnostic lines, which systemd records in the journal."""

import os
import sys
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    from buildlimits.effects import Exit
    from buildlimits.requesters import Pending

# Routine lines repeat what the metric records already hold, once per compile,
# so they are written only when an operator asks for them.
ROUTINE_LINES_VARIABLE = "BUILD_LIMITS_ROUTINE_LINES"
# systemd reads a leading ``<N>`` as the line's syslog priority when stderr is a
# journal stream, so requested routine lines stay out of ``journalctl -p info``.
DEBUG_PRIORITY = "<7>"


def log(message: str, *, routine: bool = False) -> None:
    """Write one diagnostic line for the journal.

    Parameters
    ----------
    message : str
        The line, without the supervisor's prefix.
    routine : bool
        Whether the line records healthy, high-volume work. Such a line is
        written only when ``BUILD_LIMITS_ROUTINE_LINES`` is ``1``, and then at
        debug priority when stderr is the journal.

    Examples
    --------
    >>> log("cache recovery: stopping")  # doctest: +SKIP
    build-limits-supervisor: cache recovery: stopping
    """
    if routine and os.environ.get(ROUTINE_LINES_VARIABLE) != "1":
        return
    prefix = DEBUG_PRIORITY if routine and _stderr_is_journal() else ""
    print(f"{prefix}build-limits-supervisor: {message}", file=sys.stderr, flush=True)


def _stderr_is_journal() -> bool:
    """Report whether stderr is the journal stream systemd connected.

    Returns
    -------
    bool
        True only when ``JOURNAL_STREAM`` names stderr's device and inode, so a
        terminal or a redirected file never sees a priority prefix.
    """
    device, _separator, inode = os.environ.get("JOURNAL_STREAM", "").partition(":")
    try:
        status = os.fstat(sys.stderr.fileno())
        return (status.st_dev, status.st_ino) == (int(device), int(inode))
    except AttributeError, OSError, ValueError:
        return False


def admitted_line(entry: Pending, *, cached: bool, cache_name: str = "sccache") -> str:
    """Describe an admission without its compiler arguments or directory.

    Parameters
    ----------
    entry : Pending
        The admitted request.
    cached : bool
        Whether the group runs through the configured cache backend.
    cache_name : str
        The backend's name, for the wording of a cached admission.

    Returns
    -------
    str
        The journal line.

    Notes
    -----
    The backend names itself: the default keeps sccache's established wording
    byte-identical, and a caller with another backend passes its own name.
    """
    waited = (entry.admitted or entry.arrived) - entry.arrived
    suffix = f" via {cache_name}" if cached else ""
    return (
        f"admitted request {entry.request_id} as {entry.group_id} after "
        f"{waited:.1f}s: {Path(entry.request.argv[0]).name}{suffix}"
    )


def log_finished(entry: Pending, runtime: float, exit_: Exit) -> None:
    """Record a finished request; a clean finish is a routine line.

    Parameters
    ----------
    entry : Pending
        The finished request; a recorded exec error means it never started.
    runtime : float
        Seconds from admission, or arrival, to the end.
    exit_ : Exit
        How the group's leader ended.

    Examples
    --------
    A failed compile keeps the default priority:

    >>> log_finished(entry, 1.25, Exit(code=1, signal=None))  # doctest: +SKIP
    build-limits-supervisor: finished request 7 (g3) after 1.2s: code=1 signal=None
    """
    succeeded = entry.exec_error is None and exit_.code == 0 and exit_.signal is None
    log(
        f"finished request {entry.request_id} ({entry.group_id}) after "
        f"{runtime:.1f}s: code={exit_.code} signal={exit_.signal}",
        routine=succeeded,
    )
