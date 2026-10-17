"""Control-group observation and termination for compiler scopes.

Every admitted compiler group runs in its own transient systemd scope under
the compilers slice. The unified (v2) hierarchy exposes each scope as a
directory whose ``cgroup.events`` file reports whether any process remains.
systemd removes an emptied scope's directory promptly, so a missing directory
also means an empty group.

Nested compiler adapters find their own group by reading
``/proc/self/cgroup``: a process already inside an admitted scope reuses that
group's admission instead of requesting another token.
"""

import contextlib
import errno
import itertools
import os
import signal
import typing as typ
from pathlib import Path, PurePosixPath

from buildlimits.timing import SYSTEM_CLOCK, Clock

if typ.TYPE_CHECKING:
    import collections.abc as cabc

CGROUP_ROOT = Path("/sys/fs/cgroup")
SCOPE_PREFIX = "buildlimits-"
SCOPE_SUFFIX = ".scope"
# What reading a removed control group can answer.
_GONE = frozenset({errno.ENOENT, errno.ENODEV})


def parse_unified_path(proc_cgroup: str) -> PurePosixPath | None:
    r"""Extract the unified-hierarchy path from ``/proc/<pid>/cgroup`` text.

    Parameters
    ----------
    proc_cgroup : str
        The file's contents.

    Returns
    -------
    PurePosixPath | None
        The absolute path within the hierarchy, or ``None`` when the process
        is not on a unified hierarchy.

    Examples
    --------
    >>> parse_unified_path("0::/user.slice/a.scope\n")
    PurePosixPath('/user.slice/a.scope')
    >>> parse_unified_path("1:name=systemd:/x\n") is None
    True
    """
    for line in proc_cgroup.splitlines():
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        # The unified hierarchy is the one numbered 0 with no controllers.
        is_unified = hierarchy == "0" and not controllers
        if is_unified and path.startswith("/"):
            return PurePosixPath(path)
    return None


def is_admitted_scope(path: PurePosixPath, compilers_slice: str) -> bool:
    """Decide whether a cgroup path is an admitted compiler scope or inside one.

    Parameters
    ----------
    path : PurePosixPath
        A unified-hierarchy path.
    compilers_slice : str
        The slice unit under which the supervisor creates scopes, such as
        ``buildlimits-compilers.slice``.

    Returns
    -------
    bool
        Whether a component of ``path`` is a supervisor-created scope whose
        parent is the compilers slice.

    Examples
    --------
    >>> slice_ = "buildlimits-compilers.slice"
    >>> scope = "buildlimits-7-g1.scope"
    >>> is_admitted_scope(PurePosixPath(f"/u.slice/{slice_}/{scope}"), slice_)
    True
    >>> is_admitted_scope(PurePosixPath("/u.slice/buildlimits-7-g1.scope"), slice_)
    False
    """
    parts = path.parts
    return any(
        parent == compilers_slice and is_scope_name(name)
        for parent, name in itertools.pairwise(parts)
    )


def is_scope_name(name: str) -> bool:
    """Decide whether a unit name is one the supervisor creates.

    Parameters
    ----------
    name : str
        A control-group directory or unit name.

    Returns
    -------
    bool
        Whether it has the supervisor's scope prefix and suffix.

    Examples
    --------
    >>> is_scope_name("buildlimits-1-g2.scope"), is_scope_name("other.scope")
    (True, False)
    """
    return name.startswith(SCOPE_PREFIX) and name.endswith(SCOPE_SUFFIX)


def own_path(proc: Path = Path("/proc/self/cgroup")) -> PurePosixPath | None:
    """Read the calling process's unified-hierarchy path.

    Parameters
    ----------
    proc : Path
        The ``cgroup`` file to read.

    Returns
    -------
    PurePosixPath | None
        The path, or ``None`` when it cannot be determined.
    """
    try:
        return parse_unified_path(proc.read_text(encoding="utf-8"))
    except OSError:
        return None


def directory(path: PurePosixPath, root: Path = CGROUP_ROOT) -> Path:
    """Map a hierarchy path to its directory in the mounted hierarchy.

    Parameters
    ----------
    path : PurePosixPath
        An absolute hierarchy path.
    root : Path
        The unified hierarchy's mount point.

    Returns
    -------
    Path
        The directory.
    """
    return root / path.relative_to("/")


def is_populated(group_dir: Path) -> bool:
    """Report whether any process remains in a control group.

    Parameters
    ----------
    group_dir : Path
        The group's directory.

    Returns
    -------
    bool
        ``False`` once the group is empty or has been removed.

    Raises
    ------
    OSError
        If the group's state cannot be read for any other reason. An
        unreadable group is not known to be empty, and treating it as empty
        would return its token while its processes may still run.
    """  # ruff: ignore[docstring-extraneous-exception] - re-raised by _raise_unless_gone.
    try:
        events = (group_dir / "cgroup.events").read_text(encoding="ascii")
    except OSError as error:
        _raise_unless_gone(error)
        return False
    return any(line.strip() == "populated 1" for line in events.splitlines())


def member_pids(group_dir: Path) -> list[int]:
    """List the processes in a control group and all its descendants.

    Parameters
    ----------
    group_dir : Path
        The group's directory.

    Returns
    -------
    list[int]
        Process identifiers; empty once the group is gone.

    Raises
    ------
    OSError
        If a group that still exists cannot be read: an unreadable group is
        not an empty one.
    """  # ruff: ignore[docstring-extraneous-exception] - re-raised by _raise_unless_gone.
    pids: list[int] = []
    for procs in _procs_files(group_dir):
        try:
            text = procs.read_text(encoding="ascii")
        except OSError as error:
            _raise_unless_gone(error)
            continue
        pids.extend(int(line) for line in text.split())
    return pids


def terminate(group_dir: Path, *, force: bool) -> None:
    """Signal every process in a control group.

    Parameters
    ----------
    group_dir : Path
        The group's directory.
    force : bool
        Send ``SIGKILL`` through ``cgroup.kill`` instead of ``SIGTERM`` to
        each member.
    """
    if force:
        with contextlib.suppress(OSError):
            (group_dir / "cgroup.kill").write_text("1", encoding="ascii")
        return
    for pid in member_pids(group_dir):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)


def wait_for_membership(
    pid: int, unit: str, timeout: float, clock: Clock = SYSTEM_CLOCK
) -> bool:
    """Wait until a process's control group is the named unit.

    ``StartTransientUnit`` returns once systemd has queued the job, which can
    be before the process has moved. The launch barrier must not open until
    the move has happened, or the compiler would start outside its scope.

    Parameters
    ----------
    pid : int
        The process that should move.
    unit : str
        The unit whose control group it should join.
    timeout : float
        The longest wait, in seconds.
    clock : Clock
        The clock; injectable for tests.

    Returns
    -------
    bool
        Whether the process joined the unit before the timeout.
    """
    deadline = clock.now() + timeout
    proc = Path(f"/proc/{pid}/cgroup")
    while True:
        path = own_path(proc)
        if path is not None and path.name == unit:
            return True
        if clock.now() >= deadline:
            return False
        clock.sleep(0.002)


def child_scopes(slice_dir: Path) -> list[Path]:
    """List the supervisor-created scopes under a slice.

    Parameters
    ----------
    slice_dir : Path
        The compilers slice's directory.

    Returns
    -------
    list[Path]
        Scope directories, in name order; empty when the slice is absent.
    """
    if not slice_dir.is_dir():
        return []
    return sorted(
        entry
        for entry in slice_dir.iterdir()
        if entry.is_dir() and is_scope_name(entry.name)
    )


def _procs_files(group_dir: Path) -> cabc.Iterator[Path]:
    """Yield ``cgroup.procs`` for a group and its descendants.

    Yields
    ------
    Path
        Each ``cgroup.procs`` file, the group's own first.
    """
    if not group_dir.is_dir():
        return
    for current, _dirs, files in group_dir.walk(on_error=_raise_unless_gone):
        if "cgroup.procs" in files:
            yield current / "cgroup.procs"


def _raise_unless_gone(error: OSError) -> None:
    """Re-raise a read error unless the group was removed while being read."""
    # A scope being removed can answer ENODEV instead of ENOENT.
    if error.errno not in _GONE:
        raise error
