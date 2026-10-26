"""The managed sccache server's control group, read and verified.

Each systemd unit owns one control group, and the supervisor reads that group
for two different questions: how much work is visibly running in the server,
and whether a stopped server's group is truly empty, which is the only proof
that no detached compilation can still be admitted.

Both questions share one rule -- a group path is trusted only when the manager
reported an absolute path that does not traverse a parent -- so the reading
lives here rather than in the adapter that asks it. The process-tree counter is
passed in rather than imported, so the adapter's own module binding stays the
one place a test can substitute it.

Examples
--------
>>> members("system.slice/s.service")
Traceback (most recent call last):
    ...
ValueError: manager returned an invalid server control group
>>> verify_stopped_service(
...     {"ActiveState": "active", "ControlGroup": "", "MainPID": "0"}
... )
Traceback (most recent call last):
    ...
ValueError: server unit is not in a verified stopped state
>>> verify_stopped_service(
...     {"ActiveState": "inactive", "ControlGroup": "", "MainPID": "0"}
... )
"""

import typing as typ
from pathlib import PurePosixPath

from buildlimits import cgroups
from buildlimits.cache_backend import StoppedPopulatedCacheError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    # A counter of the child trees rooted at the server's own processes.
    type Counter = cabc.Callable[[int, cabc.Sequence[int]], int]


def members(control_group: str) -> list[int]:
    """Read a validated absolute server cgroup path.

    Parameters
    ----------
    control_group : str
        The manager's reported control-group path.

    Returns
    -------
    list[int]
        Processes in the verified hierarchy path.

    Raises
    ------
    ValueError
        If the path is relative or traverses a parent directory.
    """
    path = PurePosixPath(control_group)
    if not path.is_absolute() or ".." in path.parts:
        msg = "manager returned an invalid server control group"
        raise ValueError(msg)
    return cgroups.member_pids(cgroups.directory(path))


def visible_roots(properties: cabc.Mapping[str, str], *, counter: Counter) -> int:
    """Count visible trees without authorizing detached-work token release.

    Parameters
    ----------
    properties : Mapping[str, str]
        A complete ``MainPID`` and ``ControlGroup`` snapshot.
    counter : Callable[[int, Sequence[int]], int]
        Counts the child trees rooted at the server's processes.

    Returns
    -------
    int
        Visible compiler-root trees, including orphaned roots.

    Raises
    ------
    ValueError
        If the load properties are incomplete or contradict cgroup membership.
    """  # ruff: ignore[docstring-extraneous-exception] - the private helpers raise it.
    main_pid = _server_pid(properties)
    control_group = properties["ControlGroup"]
    if not control_group:
        _verify_no_group(main_pid)
        return 0
    group_members = members(control_group)
    _verify_membership(main_pid, group_members)
    return counter(main_pid, group_members) if main_pid else 0


def verify_stopped_service(properties: cabc.Mapping[str, str]) -> None:
    """Require stopped unit state and an empty cgroup before work is accounted.

    Parameters
    ----------
    properties : Mapping[str, str]
        A complete ``ActiveState``, ``ControlGroup`` and ``MainPID`` snapshot.

    Raises
    ------
    ValueError
        If the stopped state cannot be verified.
    StoppedPopulatedCacheError
        If the service is stopped but its control group still holds processes,
        so surviving work may hold a shared jobserver slot.
    """
    if properties["ActiveState"] not in {"inactive", "failed"}:
        msg = "server unit is not in a verified stopped state"
        raise ValueError(msg)
    control_group = properties["ControlGroup"]
    if control_group and members(control_group):
        msg = "stopped server control group still contains work"
        raise StoppedPopulatedCacheError(msg)


def _server_pid(properties: cabc.Mapping[str, str]) -> int:
    """Require complete server-load properties and a non-negative PID.

    Returns
    -------
    int
        Parsed manager PID, including zero for a stopped server.

    Raises
    ------
    ValueError
        If load properties are missing or the PID is malformed.
    """
    if not {"MainPID", "ControlGroup"} <= properties.keys():
        msg = "manager omitted required server load properties"
        raise ValueError(msg)
    main_pid = int(properties["MainPID"])
    if main_pid < 0:
        msg = "manager returned a negative server PID"
        raise ValueError(msg)
    return main_pid


def _verify_no_group(main_pid: int) -> None:
    """Allow a missing cgroup only for a stopped server.

    Raises
    ------
    ValueError
        If a live server has no verified control group.
    """
    if main_pid != 0:
        msg = "a live server has no verified control group"
        raise ValueError(msg)


def _verify_membership(main_pid: int, group_members: list[int]) -> None:
    """Require an empty stopped cgroup or a live server inside its own cgroup.

    Raises
    ------
    ValueError
        If the reported server state contradicts cgroup membership.
    """
    if main_pid == 0:
        if group_members:
            msg = "stopped server control group still contains work"
            raise ValueError(msg)
        return
    if main_pid not in group_members:
        msg = "server PID is absent from its reported control group"
        raise ValueError(msg)
