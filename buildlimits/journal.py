"""Render the supervisor's diagnostic lines and write them to the journal.

Which lines are worth writing, and at which priority, is decided in
:mod:`buildlimits.journal_policy`; this module only decides how a line reads.
"""

import sys
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    from buildlimits.effects import Exit
    from buildlimits.requesters import Pending


def log(
    message: str,
    *,
    stream: typ.TextIO | None = None,
    priority_prefix: str = "",
) -> None:
    """Write one diagnostic line for the journal.

    Parameters
    ----------
    message : str
        The line, without the supervisor's prefix.
    stream : IO[str] | None
        The stream that receives the line, which the caller may have chosen
        because its device and inode decided ``priority_prefix``; standard
        error when ``None``.
    priority_prefix : str
        The leading syslog priority a journal stream parses, empty for a line
        the operator should see at the default priority.

    Examples
    --------
    >>> log("cache recovery: stopping")  # doctest: +SKIP
    build-limits-supervisor: cache recovery: stopping
    """
    print(
        f"{priority_prefix}build-limits-supervisor: {message}",
        file=sys.stderr if stream is None else stream,
        flush=True,
    )


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


def finished_line(entry: Pending, runtime: float, exit_: Exit) -> str:
    """Describe a finished request without deciding whether it is notable.

    Parameters
    ----------
    entry : Pending
        The finished request; a recorded exec error means it never started.
    runtime : float
        Seconds from admission, or arrival, to the end.
    exit_ : Exit
        How the group's leader ended.

    Returns
    -------
    str
        The journal line.

    Examples
    --------
    >>> finished_line(entry, 1.25, Exit(code=1, signal=None))  # doctest: +SKIP
    'finished request 7 (g3) after 1.2s: code=1 signal=None'
    """
    return (
        f"finished request {entry.request_id} ({entry.group_id}) after "
        f"{runtime:.1f}s: code={exit_.code} signal={exit_.signal}"
    )
