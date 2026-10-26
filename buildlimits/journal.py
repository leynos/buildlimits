"""The supervisor's diagnostic lines, which systemd records in the journal."""

import sys
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    from buildlimits.requesters import Pending


def log(message: str) -> None:
    """Write one diagnostic line for the journal."""
    print(f"build-limits-supervisor: {message}", file=sys.stderr, flush=True)


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
