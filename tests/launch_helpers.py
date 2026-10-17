"""Shared launch attributes and bounded child collection for tests."""

import os
import select
import time
import typing as typ

from descriptor_helpers import isolated

from buildlimits import attributes, launch

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def plan(
    limits: dict[str, tuple[int, int]] | None = None, *, umask: int = 0o27
) -> attributes.AttributePlan:
    """Preserve current scheduling and select caller limits and a known mask.

    Parameters
    ----------
    limits : dict[str, tuple[int, int]] or None
        Resource limits to apply; omitted limits remain unchanged.
    umask : int
        File-creation mask, defaulting to 0o27 for pressure experiments.

    Returns
    -------
    AttributePlan
        Unprivileged launch attributes for the disposable compiler.
    """
    return attributes.AttributePlan(
        umask=umask, nice=os.getpriority(os.PRIO_PROCESS, 0), rlimits=limits or {}
    )


def wait_reaped(child: launch.HeldChild, timeout: float = 1.0) -> int:
    """Poll an owned child until it ends or the bounded deadline expires.

    Parameters
    ----------
    child : HeldChild
        Launch whose exit status has not yet been collected.
    timeout : float
        Maximum polling duration in seconds.

    Returns
    -------
    int
        Raw wait status returned by the launch.

    Raises
    ------
    AssertionError
        If the child has not exited before the deadline.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = child.reap()
        if status is not None:
            return status
        time.sleep(0.005)
    msg = f"child {child.pid} was not reaped within {timeout}s"
    raise AssertionError(msg)


def outcome(child: launch.HeldChild) -> tuple[int, str | None]:
    """Collect a bounded setup outcome and reclaim the owned launch.

    Parameters
    ----------
    child : HeldChild
        Disposable launch with an error channel.

    Returns
    -------
    tuple[int, str or None]
        Exit code and the setup diagnostic, if present.

    Raises
    ------
    AssertionError
        If setup or child exit exceeds the ten-second deadline.
    """
    try:
        if child.errors is None:
            msg = "the launch owns an error channel"
            raise AssertionError(msg)
        message = _setup_outcome(child)
        status = wait_reaped(child, timeout=10)
        return os.waitstatus_to_exitcode(status), message
    finally:
        if not child.reaped:
            child.abandon()
        child.close()


def _setup_outcome(child: launch.HeldChild) -> str | None:
    """Wait only for launch setup, retaining partial diagnostics until EOF.

    Returns
    -------
    str | None
        Complete setup diagnostic or a successful exec boundary.

    Raises
    ------
    AssertionError
        If the error channel does not complete within ten seconds.
    """
    deadline = time.monotonic() + 10
    message = None
    while child.errors is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = "launch setup completes within the bounded deadline"
            raise AssertionError(msg)
        ready, _, _ = select.select([child.errors], [], [], remaining)
        if not ready:
            msg = "the launch channel becomes ready before its deadline"
            raise AssertionError(msg)
        message = launch.read_exec_outcome(child)
    return message


def clean_isolated_launch(action: cabc.Callable[[int], object]) -> dict[str, object]:
    """Run a launch experiment in a disposable process and require a clean exit.

    Parameters
    ----------
    action : Callable[[int], object]
        The experiment, given the descriptor it reports through.

    Returns
    -------
    dict[str, object]
        The experiment's report, for its own further checks.

    Raises
    ------
    TypeError
        If the experiment reported no structured evidence.
    AssertionError
        If the experiment reported a non-zero exit or a setup diagnostic.
    """
    result = isolated(action)
    if not isinstance(result, dict):
        msg = f"the isolated process must report structured evidence, got {result!r}"
        raise TypeError(msg)
    if result["code"] != 0 or result["message"] is not None:
        raise AssertionError(result)
    return result
