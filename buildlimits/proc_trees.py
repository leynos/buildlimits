"""Compiler-root tree counting from ``/proc``.

A cache-miss compilation runs as a direct child of the sccache server, outside
any compiler scope, and must stay covered by a held token. These helpers count
the distinct process trees among a server cgroup's members without assuming
the server still exists: a surviving orphan root is work even when its parent
has gone. Counting is diagnostic only; no count here can authorize returning a
token, because a live cache may hold accepted requests that have not yet
spawned anything.
"""

import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

PROC = Path("/proc")
_STARTTIME_FIELD_AFTER_COMM = 19


def parent_pid(stat_text: str) -> int:
    """Extract the parent process identifier from ``/proc/<pid>/stat`` text.

    The command name is parenthesized and may contain spaces or parentheses,
    so the fields are counted from the last closing parenthesis.

    Parameters
    ----------
    stat_text : str
        The file's contents.

    Returns
    -------
    int
        The parent process identifier.

    Examples
    --------
    >>> parent_pid("123 (rustc (x) y) S 77 123 123 0 -1")
    77
    """
    after_name = stat_text[stat_text.rindex(")") + 1 :].split()
    return int(after_name[1])


def process_starttime(stat_text: str) -> int:
    """Extract the non-negative field 22 start time from a proc stat row.

    The command name can contain spaces and parentheses, so fields are counted
    only after the final closing parenthesis. Callers retain their own policy
    for unreadable files and malformed rows; an invalid row surfaces the
    helpers' ``ValueError`` rather than a substituted default.

    Parameters
    ----------
    stat_text : str
        The contents of ``/proc/<pid>/stat``.

    Returns
    -------
    int
        The process start time in clock ticks.

    Examples
    --------
    >>> process_starttime("123 (rustc (x) y) S " + " ".join(["0"] * 18 + ["456"]))
    456
    """
    return _non_negative_starttime(_starttime_field(stat_text))


def _starttime_field(stat_text: str) -> str:
    """Return the raw start-time field from a proc stat row.

    Returns
    -------
    str
        The field's text, before integer conversion.

    Raises
    ------
    ValueError
        If the command field or start-time field is missing.

    Examples
    --------
    >>> _starttime_field("123 (rustc) S " + " ".join(["0"] * 18 + ["456"]))
    '456'
    """
    _command, separator, after_command = stat_text.rpartition(")")
    if not separator:
        msg = "process stat omitted its parenthesized command"
        raise ValueError(msg)
    fields = after_command.split()
    if len(fields) <= _STARTTIME_FIELD_AFTER_COMM:
        msg = "process stat omitted its start time"
        raise ValueError(msg)
    return fields[_STARTTIME_FIELD_AFTER_COMM]


def _non_negative_starttime(field: str) -> int:
    """Convert a start-time field, rejecting negative or non-numeric values.

    Returns
    -------
    int
        The non-negative start time in clock ticks.

    Raises
    ------
    ValueError
        If the field is not an integer or is negative.

    Examples
    --------
    >>> _non_negative_starttime("456")
    456
    """
    try:
        starttime = int(field)
    except ValueError as error:
        msg = "process stat has an invalid start time"
        raise ValueError(msg) from error
    if starttime < 0:
        msg = "process start time cannot be negative"
        raise ValueError(msg)
    return starttime


def read_process_starttime(pid: int, proc: Path = PROC) -> int:
    """Read one process identity's start time from an injectable proc root.

    Parameters
    ----------
    pid : int
        Process identifier whose stat row is read.
    proc : Path
        Proc filesystem root, normally ``/proc``.

    Returns
    -------
    int
        The validated start time in clock ticks.

    Raises
    ------
    OSError
        If the process stat file cannot be read.
    UnicodeError
        If the stat file is not ASCII.
    ValueError
        If field 22 is missing, malformed or negative.

    Examples
    --------
    ``read_process_starttime(123, fixture_proc)`` reads
    ``fixture_proc/123/stat`` and applies the same field validation used by
    production process identity checks.
    """  # ruff: ignore[docstring-extraneous-exception] - read_text and process_starttime propagate these failures.
    stat_text = (proc / str(pid) / "stat").read_text(encoding="ascii")
    return process_starttime(stat_text)


def count_children(parent: int, members: cabc.Iterable[int], proc: Path = PROC) -> int:
    """Count compiler-root trees, including orphaned work, among ``members``.

    Parameters
    ----------
    parent : int
        The server's process identifier.
    members : Iterable[int]
        Candidate processes, normally the server unit's cgroup members.
    proc : Path
        The proc filesystem.

    Returns
    -------
    int
        Direct server children and orphan roots; their descendants share the
        root's token. Processes that exit during inspection are skipped.

    Raises
    ------
    OSError
        If a live process's state cannot be read.
    ValueError
        If a process's state cannot be parsed.
    """  # ruff: ignore[docstring-extraneous-exception] - raised by reading and parsing /proc.
    parents = _process_parents(parent, members, proc)
    # Orphan roots remain work even when the server is alive. Count trees,
    # not grandchildren such as linkers, so one tree needs one retained token.
    roots = [pid for pid, owner in parents.items() if owner not in parents]
    for pid in parents:
        _verify_parent_chain(pid, parents)
    return len(roots)


def _process_parents(
    server: int, members: cabc.Iterable[int], proc: Path
) -> dict[int, int]:
    """Read live member parentage while tolerating processes that have exited.

    Returns
    -------
    dict[int, int]
        Parent identifiers for live members other than the server.
    """
    parents: dict[int, int] = {}
    for pid in set(members):
        if pid == server:
            continue
        try:
            stat = (proc / str(pid) / "stat").read_text(encoding="utf-8")
        except FileNotFoundError, ProcessLookupError:
            continue
        parents[pid] = parent_pid(stat)
    return parents


def _verify_parent_chain(pid: int, parents: dict[int, int]) -> None:
    """Reject cycles before a process-tree count can look idle.

    Raises
    ------
    ValueError
        If a process's parent chain visits the same member twice.
    """
    seen = set()
    current = pid
    while current in parents:
        if current in seen:
            msg = "server cgroup has a cyclic process parent chain"
            raise ValueError(msg)
        seen.add(current)
        current = parents[current]
