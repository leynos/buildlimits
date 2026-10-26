"""Stop a live managed cache at start-up, under the restart barrier.

A supervisor that exits leaves the managed cache running: the cache is a
separate unit with its own ``Restart=always``. The next supervisor then finds a
live server, and a live server may hold accepted requests that wait for a
jobserver token, so the strict accounting check refuses it. That refusal is
right for a cache whose users are unknown, and wrong for this one: by the time
the check runs, start-up has terminated every compiler scope under the
compilers slice and holds the singleton lock, so no admitted group can still be
talking to the cache. What remains in it is detached work, which is exactly
what the recovery transaction stops the cache to account for.

This module therefore runs that transaction's first half before the pool
opens: write the barrier, queue the stop, and wait for the strict check itself
to prove the service stopped and empty. The strict check stays the only
authority; this code adds a stop in front of it and never reads a count, an
idle CPU or a missing process as proof. The barrier is left in place on
success, because the reclamation that runs next retires it and starts the
cache, and left in place on failure, because an unaccounted cache must not
return.
"""

import typing as typ

from buildlimits.cache_backend import (
    CacheError,
    LiveCacheWorkError,
    StartupAccountingError,
)
from buildlimits.journal import log
from buildlimits.systemd import SystemdError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.recovery_barrier import RecoveryBarrier
    from buildlimits.sccache import ManagedSccache
    from buildlimits.timing import Clock

# Bounded so that it and the 120-second scope drain still fit inside the
# supervisor unit's 180-second TimeoutStartSec: a cache which will not stop then
# fails start-up with the operator guidance, not with a timeout that systemd
# reports without saying why.
QUIESCE_TIMEOUT_SECONDS = 45.0
QUIESCE_POLL_SECONDS = 0.2
_STOPPING = (
    "start-up found a live managed cache with no admitted group left to use it; "
    "stopping it under the restart barrier"
)


class QuiesceTimeoutError(StartupAccountingError):
    """The stopped cache was not verified stopped and empty in time."""


def quiesce_and_report(
    cache: ManagedSccache,
    barrier: RecoveryBarrier,
    clock: Clock,
    report: cabc.Callable[[str, float], None],
) -> None:
    """Run :func:`quiesce_live_cache` and report how it ended, once, if it acted.

    Parameters
    ----------
    cache : ManagedSccache
        The managed server.
    barrier : RecoveryBarrier
        The restart barrier the unit's own condition reads.
    clock : Clock
        Monotonic time and sleep; injectable for tests.
    report : Callable[[str, float], None]
        Receives the outcome token (``stopped``, ``refused`` or ``timed_out``)
        and the seconds spent. Nothing is reported when the cache was not live.

    Raises
    ------
    QuiesceTimeoutError
        If the stopped cache was not verified in time, after reporting it.
    StartupAccountingError
        As :func:`quiesce_live_cache`, after the outcome has been reported.
    """
    started = clock.now()
    try:
        acted = quiesce_live_cache(cache, barrier, clock)
    except QuiesceTimeoutError:
        report("timed_out", clock.now() - started)
        raise
    except StartupAccountingError:
        report("refused", clock.now() - started)
        raise
    if acted:
        report("stopped", clock.now() - started)


def quiesce_live_cache(
    cache: ManagedSccache,
    barrier: RecoveryBarrier,
    clock: Clock,
    *,
    timeout: float = QUIESCE_TIMEOUT_SECONDS,
) -> bool:
    """Stop a live cache and wait until it is verified stopped and empty.

    Only a cache the manager reports live is acted on. A stopped cache needs
    nothing, and any other reading — a stopped service that still holds
    processes, or a manager that cannot be read — is left for the strict check
    that follows to refuse, because stopping a cache whose state is unreadable
    would be a guess.

    Parameters
    ----------
    cache : ManagedSccache
        The managed server.
    barrier : RecoveryBarrier
        The restart barrier the unit's own condition reads.
    clock : Clock
        Monotonic time and sleep; injectable for tests.
    timeout : float
        The longest wait for the stop to complete, in seconds.

    Returns
    -------
    bool
        Whether a live cache was stopped and verified.

    Raises
    ------
    StartupAccountingError
        If the barrier cannot be written, the stop is refused, or the cache is
        not verified stopped and empty within the timeout.
    """  # ruff: ignore[docstring-extraneous-exception] - its helpers raise it.
    try:
        cache.accounted_drain()
    except LiveCacheWorkError:
        pass
    except CacheError:
        return False
    else:
        return False
    log(_STOPPING)
    _stop_under_barrier(cache, barrier)
    _await_stopped(cache, clock, timeout)
    return True


def _stop_under_barrier(cache: ManagedSccache, barrier: RecoveryBarrier) -> None:
    """Write the barrier, then queue the stop; never the stop without the barrier.

    Raises
    ------
    StartupAccountingError
        If the barrier cannot be written, or the manager refuses the stop.
    """
    for step in (barrier.write, cache.stop):
        try:
            step()
        except (OSError, SystemdError) as error:
            msg = f"cannot stop the live managed cache at start-up: {error}"
            raise StartupAccountingError(msg) from error


def _await_stopped(cache: ManagedSccache, clock: Clock, timeout: float) -> None:
    """Poll the strict accounting check until it passes or the timeout expires.

    Raises
    ------
    QuiesceTimeoutError
        If the service is not verified stopped and empty in time. The last
        reading is reported, because it says whether the unit was still
        stopping, held surviving processes, or could not be read.
    """
    deadline = clock.now() + timeout
    while True:
        try:
            cache.accounted_drain()
        except CacheError as error:
            last = error
        else:
            return
        if clock.now() >= deadline:
            msg = (
                f"startup stopped the live managed cache but could not verify it "
                f"stopped and empty within {timeout:g}s: {last}; operator action: "
                "inspect the cache's control group, wait for it to empty, then "
                "start the supervisor"
            )
            raise QuiesceTimeoutError(msg) from last
        clock.sleep(QUIESCE_POLL_SECONDS)
