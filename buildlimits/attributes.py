"""Caller process attributes that a supervisor-forked compiler must inherit.

The supervisor, not Cargo, forks each compiler, so without intervention the
compiler would run with the supervisor's umask, niceness and resource limits.
The adapter captures the caller's values; the launch child applies them before
``exec``.

An unprivileged process can lower a hard resource limit but not raise one,
and can lower its priority but not raise it. :func:`plan_attributes` decides,
without side effects, which caller attributes are reachable and refuses
anything unreachable. A compiler never silently inherits supervisor attributes.
"""

import dataclasses as dc
import os
import resource
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

INFINITY = -1
"""The portable form of ``resource.RLIM_INFINITY``."""

RLIMIT_NAMES: tuple[str, ...] = tuple(
    name
    for name in (
        "RLIMIT_AS",
        "RLIMIT_CORE",
        "RLIMIT_CPU",
        "RLIMIT_DATA",
        "RLIMIT_FSIZE",
        "RLIMIT_LOCKS",
        "RLIMIT_MEMLOCK",
        "RLIMIT_MSGQUEUE",
        "RLIMIT_NICE",
        "RLIMIT_NOFILE",
        "RLIMIT_NPROC",
        "RLIMIT_RSS",
        "RLIMIT_RTPRIO",
        "RLIMIT_RTTIME",
        "RLIMIT_SIGPENDING",
        "RLIMIT_STACK",
    )
    if hasattr(resource, name)
)


class AttributeRefusalError(Exception):
    """A caller attribute cannot be reproduced without privilege."""


class AttributeCaptureError(Exception):
    """The process's attributes cannot be read without changing them."""


@dc.dataclass(frozen=True, slots=True)
class AttributePlan:
    """The attributes the launch child will set.

    Attributes
    ----------
    umask : int
        The file-creation mask to set.
    nice : int
        The niceness to set.
    rlimits : dict[str, tuple[int, int]]
        The ``(soft, hard)`` limit to set per resource, in portable form.
    """

    umask: int
    nice: int
    rlimits: dict[str, tuple[int, int]]


@dc.dataclass(frozen=True, slots=True)
class ProcessAttributes:
    """Caller process attributes that a forked compiler would otherwise lose.

    Attributes
    ----------
    umask : int
        The caller's file-creation mask.
    nice : int
        The caller's scheduling niceness.
    rlimits : dict[str, tuple[int, int]]
        Resource limits by ``resource`` module name, as ``(soft, hard)``;
        ``resource.RLIM_INFINITY`` is carried as ``-1``.
    """

    umask: int
    nice: int
    rlimits: dict[str, tuple[int, int]]


def at_most(left: int, right: int) -> bool:
    """Compare two limits in portable form, where ``-1`` is infinity.

    Parameters
    ----------
    left : int
        A limit.
    right : int
        Another limit.

    Returns
    -------
    bool
        Whether ``left`` does not exceed ``right``.

    Examples
    --------
    >>> at_most(5, INFINITY), at_most(INFINITY, 5), at_most(INFINITY, INFINITY)
    (True, False, True)
    """
    if right == INFINITY:
        return True
    if left == INFINITY:
        return False
    return left <= right


def plan_limit(caller: tuple[int, int], current: tuple[int, int]) -> tuple[int, int]:
    """Plan one resource limit for the compiler.

    The caller's hard limit is kept when the supervisor can reach it, and
    otherwise capped at the supervisor's; the caller's soft limit is always
    kept.

    Parameters
    ----------
    caller : tuple[int, int]
        The caller's ``(soft, hard)`` limit, in portable form.
    current : tuple[int, int]
        The launch child's current ``(soft, hard)`` limit, in portable form.

    Returns
    -------
    tuple[int, int]
        The ``(soft, hard)`` limit to set.

    Raises
    ------
    AttributeRefusalError
        If the caller's soft limit exceeds what the launch child can set.

    Examples
    --------
    >>> plan_limit((1024, 4096), (1024, INFINITY))
    (1024, 4096)
    >>> plan_limit((1024, INFINITY), (1024, 4096))
    (1024, 4096)
    """
    caller_soft, caller_hard = caller
    current_hard = current[1]
    hard = caller_hard if at_most(caller_hard, current_hard) else current_hard
    if not at_most(caller_soft, hard):
        msg = f"soft limit {caller_soft} exceeds the reachable hard limit {hard}"
        raise AttributeRefusalError(msg)
    return caller_soft, hard


def plan_attributes(
    caller: ProcessAttributes,
    current_nice: int,
    current_limits: cabc.Mapping[str, tuple[int, int]],
) -> AttributePlan:
    """Plan every attribute the launch child sets.

    Parameters
    ----------
    caller : ProcessAttributes
        The attributes the adapter captured.
    current_nice : int
        The launch child's current niceness.
    current_limits : Mapping[str, tuple[int, int]]
        The launch child's current limits, in portable form.

    Returns
    -------
    AttributePlan
        The attributes to set.

    Raises
    ------
    AttributeRefusalError
        If the caller's priority is higher than the launch child's, a soft
        limit is out of reach, or a limit names an unknown resource.
    """
    if caller.nice < current_nice:
        msg = f"cannot raise priority from niceness {current_nice} to {caller.nice}"
        raise AttributeRefusalError(msg)
    limits: dict[str, tuple[int, int]] = {}
    for name, pair in caller.rlimits.items():
        if name not in current_limits:
            msg = f"unknown resource limit {name!r}"
            raise AttributeRefusalError(msg)
        limits[name] = plan_limit(pair, current_limits[name])
    return AttributePlan(umask=caller.umask & 0o777, nice=caller.nice, rlimits=limits)


def capture() -> ProcessAttributes:
    """Capture this process's umask, niceness and resource limits.

    Returns
    -------
    ProcessAttributes
        The current attributes, in portable form.
    """
    return ProcessAttributes(
        umask=current_umask(),
        nice=os.getpriority(os.PRIO_PROCESS, 0),
        rlimits=current_limits(),
    )


def current_umask() -> int:
    """Read the umask without changing it.

    ``os.umask`` can only read by setting, which would race with other
    threads, so the value comes only from ``/proc/self/status``. A kernel
    that does not report it there fails the capture rather than guessing.

    Returns
    -------
    int
        The file-creation mask.

    Raises
    ------
    AttributeCaptureError
        If ``/proc/self/status`` has no valid ``Umask`` field.
    """
    for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
        if line.startswith("Umask:"):
            field = line.split(":", 1)[1].strip()
            try:
                return int(field, 8)
            except ValueError as error:
                msg = f"cannot read the umask from {field!r}"
                raise AttributeCaptureError(msg) from error
    msg = "cannot read the umask: /proc/self/status has no Umask field"
    raise AttributeCaptureError(msg)


def current_limits() -> dict[str, tuple[int, int]]:
    """Read every known resource limit, in portable form.

    Returns
    -------
    dict[str, tuple[int, int]]
        ``(soft, hard)`` per resource name.
    """
    return {
        name: _to_portable(resource.getrlimit(getattr(resource, name)))
        for name in RLIMIT_NAMES
    }


def _to_portable(pair: tuple[int, int]) -> tuple[int, int]:
    """Represent ``RLIM_INFINITY`` as ``-1``.

    Returns
    -------
    tuple[int, int]
        The pair in portable form.
    """
    soft, hard = pair
    return _value_to_portable(soft), _value_to_portable(hard)


def _value_to_portable(value: int) -> int:
    """Map one limit to portable form.

    Returns
    -------
    int
        ``-1`` for infinity, otherwise the value.
    """
    return INFINITY if value == resource.RLIM_INFINITY else value
