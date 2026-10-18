"""Supervisor start-up: the singleton lock and the restart barrier.

A new supervisor must not admit work from a fresh token pool while compiler
groups from a previous instance survive. Guildmaster returns a crashed
supervisor's tokens as soon as its handle closes, so those survivors would
otherwise run outside the limit. Before opening the pool, every start
therefore terminates every scope under the compilers slice and waits for the
slice to empty; failure to drain prevents start-up. Scopes are found in the
control-group hierarchy, not in any process map lost with the old instance.
"""

import fcntl
import os
from pathlib import Path, PurePosixPath

from buildlimits import cgroups
from buildlimits.timing import SYSTEM_CLOCK, Clock


class StartupError(Exception):
    """The supervisor cannot safely start."""


def acquire_lock(path: Path) -> int:
    """Take the supervisor's singleton lock.

    Parameters
    ----------
    path : Path
        The lock file; its directory is created private to the owner.

    Returns
    -------
    int
        The locked descriptor, held for the supervisor's lifetime.

    Raises
    ------
    StartupError
        If another supervisor holds the lock.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException as error:
        os.close(fd)
        if isinstance(error, BlockingIOError):
            msg = f"another supervisor holds {path}"
            raise StartupError(msg) from error
        raise
    return fd


def user_manager_root(own: PurePosixPath) -> PurePosixPath:
    """Find the user manager's subtree from the supervisor's own cgroup.

    Parameters
    ----------
    own : PurePosixPath
        The supervisor's unified-hierarchy path.

    Returns
    -------
    PurePosixPath
        The path of the ``user@<uid>.service`` directory.

    Raises
    ------
    StartupError
        If the supervisor is not running under a systemd user manager.

    Examples
    --------
    >>> manager = "/user.slice/user-1000.slice/user@1000.service"
    >>> user_manager_root(PurePosixPath(f"{manager}/buildlimits.slice/s.service"))
    PurePosixPath('/user.slice/user-1000.slice/user@1000.service')
    """
    parts = own.parts
    for index, part in enumerate(parts):
        if part.startswith("user@") and part.endswith(".service"):
            return PurePosixPath(*parts[: index + 1])
    msg = "the supervisor must run as a systemd user service"
    raise StartupError(msg)


def slice_path(root: PurePosixPath, slice_unit: str) -> PurePosixPath:
    """Map a slice unit name to its path, following systemd's dash nesting.

    Parameters
    ----------
    root : PurePosixPath
        The user manager's subtree.
    slice_unit : str
        A slice unit such as ``buildlimits-compilers.slice``.

    Returns
    -------
    PurePosixPath
        Its path in the hierarchy.

    Examples
    --------
    >>> slice_path(PurePosixPath("/u"), "buildlimits-compilers.slice")
    PurePosixPath('/u/buildlimits.slice/buildlimits-compilers.slice')
    """
    stem = slice_unit.removesuffix(".slice")
    pieces = stem.split("-")
    path = root
    for count in range(1, len(pieces) + 1):
        path /= "-".join(pieces[:count]) + ".slice"
    return path


def drain_slice(slice_dir: Path, timeout: float, clock: Clock = SYSTEM_CLOCK) -> int:
    """Kill every surviving compiler scope and wait for the slice to empty.

    Parameters
    ----------
    slice_dir : Path
        The compilers slice's directory.
    timeout : float
        The longest wait, in seconds.
    clock : Clock
        The clock; injectable for tests.

    Returns
    -------
    int
        The number of surviving scopes found.

    Raises
    ------
    StartupError
        If any scope still holds a process when the timeout expires.
    """
    survivors = cgroups.child_scopes(slice_dir)
    for scope in survivors:
        cgroups.terminate(scope, force=True)
    deadline = clock.now() + timeout
    while True:
        populated = [scope for scope in survivors if cgroups.is_populated(scope)]
        if not populated:
            return len(survivors)
        if clock.now() >= deadline:
            names = ", ".join(scope.name for scope in populated)
            msg = f"compiler scopes did not drain within {timeout}s: {names}"
            raise StartupError(msg)
        clock.sleep(0.1)
