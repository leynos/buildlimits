"""Supervisor-only periodic checks; admission retains all token and exit authority.

The event loop owns the deadlines and invokes these checks in server, pool,
then cache order. A failed check propagates through the existing drain loop.
"""

import typing as typ

from buildlimits import kache_guard

if typ.TYPE_CHECKING:
    from buildlimits.pool import TokenPool
    from buildlimits.supervisor import Supervisor

SERVER_POLL_SECONDS = 1.0
HEALTH_POLL_SECONDS = 2.0
CACHE_POLL_SECONDS = 5.0


def poll_services(supervisor: Supervisor, now: float) -> None:
    """Apply due server, pool and cache checks in their established order.

    Parameters
    ----------
    supervisor : Supervisor
        Event-loop owner with admission effects and periodic deadlines.
    now : float
        Single monotonic sample taken after compiler-group recovery.
    """
    _poll_server(supervisor, now)
    _poll_pool(supervisor, now)
    _poll_cache(supervisor, now)


def _poll_server(supervisor: Supervisor, now: float) -> None:
    """Check accounted server drains before allowing health or cache transitions."""
    if now >= supervisor._next_server_poll:
        supervisor._next_server_poll = now + SERVER_POLL_SECONDS
        # The drain probe keeps sole authority to return a server-drain token,
        # and it reads the cache's state before recovery can ask for a stop.
        # Recovery therefore runs afterwards: a token that this poll can
        # account for is returned without stopping a cache that is idle.
        supervisor._apply(supervisor.server.drained_tokens())
        supervisor.recovery.tick(now)


def _poll_pool(supervisor: Supervisor, now: float) -> None:
    """Treat replacement of the owned pool device as an unhealthy transition."""
    if now < supervisor._next_health_poll:
        return
    supervisor._next_health_poll = now + HEALTH_POLL_SECONDS
    pool: TokenPool | None = supervisor.pool
    if pool is not None and not pool.is_same_device():
        if supervisor.admission.healthy:
            supervisor._metric("pool_failure", operation="device")
        supervisor._unhealthy(f"{supervisor.settings.device} was replaced")


def _poll_cache(supervisor: Supervisor, now: float) -> None:
    """Refresh cache readiness and the stray-process guard.

    The guard runs on this cadence rather than the one-second server poll
    because it reads every scope in the slice: a stray is a report, and a
    report half a minute late is the same report.
    """
    if now >= supervisor._next_cache_poll:
        supervisor._next_cache_poll = now + CACHE_POLL_SECONDS
        supervisor.server.refresh_cache_mode()
        kache_guard.refresh_guard(supervisor.groups)
