"""File-descriptor inheritance for admitted compilers.

A compiler that Cargo starts directly inherits every descriptor Cargo left
without ``FD_CLOEXEC``: its standard streams and, when Cargo shares a
jobserver, the jobserver's pipe ends. The adapter sends exactly that set to
the supervisor, and the launch child recreates each at its original number
before executing the compiler. Environment strings such as
``CARGO_MAKEFLAGS`` therefore stay valid without being parsed or rewritten,
and FIFO jobservers, which are named by path, need nothing at all.

Moving received descriptors onto their target numbers must not clobber a
descriptor that still has to move. :func:`relocation_plan` computes a safe
order as data; :func:`apply_relocation` carries it out.
"""

import contextlib
import fcntl
import os
import resource
import typing as typ
from pathlib import Path

from buildlimits.relocation import Close, Copy, RelocationPlan, relocation_plan

if typ.TYPE_CHECKING:
    from buildlimits.launch_setup import Setup

if typ.TYPE_CHECKING:
    import collections.abc as cabc

PROC_SELF_FD = Path("/proc/self/fd")


def inheritable_descriptors(exclude: cabc.Collection[int] = ()) -> list[int]:
    """List this process's descriptors that an ``exec`` would inherit.

    Parameters
    ----------
    exclude : Collection[int]
        Descriptors to leave out, such as the supervisor connection.

    Returns
    -------
    list[int]
        Open descriptors without ``FD_CLOEXEC``, in ascending order.
    """
    found: list[int] = []
    for entry in PROC_SELF_FD.iterdir():
        fd = int(entry.name)
        if fd in exclude:
            continue
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        except OSError:
            # The directory's own descriptor, already closed.
            continue
        if not flags & fcntl.FD_CLOEXEC:
            found.append(fd)
    return sorted(found)


def apply_relocation(
    received: cabc.Sequence[int],
    targets: cabc.Sequence[int],
    *,
    keep: cabc.Sequence[int] = (),
    setup: Setup | None = None,
) -> list[int]:
    """Move received descriptors onto their targets and close everything else.

    Run in the launch child just before ``exec``. Afterwards the process holds
    exactly the target descriptors, all inheritable, and the ``keep``
    descriptors, moved out of target slots and made close-on-exec.

    Parameters
    ----------
    received : Sequence[int]
        The descriptors the child received.
    targets : Sequence[int]
        The number each must occupy.
    keep : Sequence[int]
        Descriptors the child still needs until ``exec``, such as its error
        pipe. They may currently sit on a target number.
    setup : Setup | None
        Launch diagnostic state and mutable writer ownership. Its caller has
        already reclaimed inherited descriptors and restores limits later;
        direct callers reclaim descriptors and restore limits here.

    Returns
    -------
    list[int]
        The new numbers of the ``keep`` descriptors, in the same order.
    """
    limits = resource.getrlimit(resource.RLIMIT_NOFILE)
    if setup is not None:
        setup.stage = "relocation-limit"
    plan = relocation_plan(received, targets, keep=keep, hard=limits[1])
    for fd in [*received, *keep]:
        os.fstat(fd)
    kept = list(keep) if setup is None else setup.kept
    if setup is not None:
        setup.required_soft = plan.required_soft
    else:
        close_unrelated({*received, *keep})
    try:
        _prepare_limit(limits, plan.required_soft, setup)
        if setup is not None:
            setup.stage = "relocate"
        execute_plan(plan, kept)
    finally:
        if setup is None:
            resource.setrlimit(resource.RLIMIT_NOFILE, limits)
    return kept


def execute_plan(plan: RelocationPlan, kept: list[int]) -> None:
    """Execute a plan, updating retained channels after every successful copy.

    Parameters
    ----------
    plan : RelocationPlan
        Validated operations from the pure relocation planner.
    kept : list[int]
        Mutable channel locations, kept correct even after partial failure.
    """
    for step in plan.steps:
        match step:
            case Copy():
                _copy(step, kept)
            case Close(fd=fd):
                os.close(fd)


def _copy(step: Copy, kept: list[int]) -> None:
    """Apply one copy and update channel ownership only after success."""
    if step.source == step.target:
        os.set_inheritable(step.target, step.inheritable)
    else:
        os.dup2(step.source, step.target, inheritable=step.inheritable)
        kept[:] = [step.target if fd == step.source else fd for fd in kept]


def _prepare_limit(limits: tuple[int, int], required: int, setup: Setup | None) -> None:
    """Raise only the validated relocation ceiling and track successful changes."""
    if setup is not None:
        setup.stage = "relocation-limit"
    if limits[0] != -1 and required > limits[0]:
        raised = (required, limits[1])
        resource.setrlimit(resource.RLIMIT_NOFILE, raised)
        if setup is not None:
            setup.limits = raised


def open_descriptors_except(wanted: cabc.Collection[int]) -> list[int]:
    """List open descriptors that are not in ``wanted``.

    Parameters
    ----------
    wanted : Collection[int]
        Descriptors to keep.

    Returns
    -------
    list[int]
        Every other open descriptor.
    """
    # Read the directory fully before the caller closes anything: the listing
    # holds a descriptor of its own while it is open.
    listed = [int(entry.name) for entry in PROC_SELF_FD.iterdir()]
    unwanted: list[int] = []
    for fd in listed:
        if fd in wanted:
            continue
        try:
            os.fstat(fd)
        except OSError:
            continue
        unwanted.append(fd)
    return unwanted


def close_all(fds: cabc.Iterable[int]) -> None:
    """Close descriptors, ignoring any already closed.

    Parameters
    ----------
    fds : Iterable[int]
        The descriptors.
    """
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


def close_unrelated(wanted: cabc.Collection[int]) -> None:
    """Reclaim unrelated inherited descriptors before allocating relocation slots.

    Parameters
    ----------
    wanted : Collection[int]
        Owned sources and setup channels that must survive.
    """
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    upper = max(
        soft if soft >= 0 else os.sysconf("SC_OPEN_MAX"), max(wanted, default=-1) + 1
    )
    start = 0
    for fd in sorted(set(wanted)):
        os.closerange(start, fd)
        start = fd + 1
    os.closerange(start, upper)
    # uv interpreters can implement closerange with individual close calls.
    # Reclaim low slots first, then enumerate inherited descriptors above a
    # lowered limit rather than scanning up to INT_MAX or allocating first.
    close_all(open_descriptors_except(wanted))
