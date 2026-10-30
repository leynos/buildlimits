"""Construct the cache watch and the recovery machine wired to it.

The two objects answer each other: recovery asks the watch for its live
holdings, while the watch asks recovery whether automatic recovery is already
pending. Neither can be built first in full, so this module owns the wiring and
the one late-resolved probe that keeps the cycle lazy.
"""

import dataclasses as dc
import typing as typ

from buildlimits import cache_recovery
from buildlimits.sccache import ManagedSccache
from buildlimits.server_watch import ServerWatch

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits import config
    from buildlimits.admission import Admission
    from buildlimits.metrics import MetricsPort
    from buildlimits.systemd import UserManager


@dc.dataclass(frozen=True, slots=True)
class Wiring:
    """Construction inputs shared by the cache watch and the recovery machine.

    Attributes
    ----------
    settings : config.SupervisorConfig
        The validated supervisor configuration.
    manager : UserManager
        The user manager port used to drive the cache unit.
    clock : Callable[[], float]
        Monotonic time, read per call by every late probe.
    admission : Admission
        Admission books; recovery reports its token counts here.
    metrics : MetricsPort
        Event metrics shared by the watch and recovery.
    """

    settings: config.SupervisorConfig
    manager: UserManager
    clock: cabc.Callable[[], float]
    admission: Admission
    metrics: MetricsPort


def watch_and_recovery(
    wiring: Wiring,
) -> tuple[ServerWatch, cache_recovery.CacheRecovery]:
    """Build the cache watch, then the recovery machine wired to it.

    The watch is built first because recovery reads its live holdings, so the
    pending probe reached through ``holder`` is resolved late, per call, and
    sees the finished recovery machine.

    Returns
    -------
    tuple[ServerWatch, cache_recovery.CacheRecovery]
        The cache watch, then the recovery machine.

    Examples
    --------
    ``watch_and_recovery(Wiring(settings, manager, clock, admission, metrics))``
    returns a watch whose ``automatic_recovery_pending`` reads the recovery
    machine built in the same call.
    """
    settings = wiring.settings
    server = ManagedSccache(
        settings.sccache_unit, settings.sccache_socket, wiring.manager
    )
    holder: list[cache_recovery.CacheRecovery] = []

    def automatic_recovery_pending() -> bool:
        """Report whether recovery has work it would start without demand.

        Returns
        -------
        bool
            Whether the recovery machine reports a pending automatic step.
        """
        return bool(holder[0].describe(wiring.clock())["pending"])

    watch = ServerWatch(
        server,
        wiring.admission,
        settings.cache_mode,
        clock=wiring.clock,
        metrics=wiring.metrics,
        automatic_recovery_pending=automatic_recovery_pending,
    )
    # Installed once the watch exists, because the permission it answers for
    # cannot be read before then. A bound method rather than a closure or a
    # forwarding method: the reference is resolved per call, so the watch's
    # later decisions are the ones that count, with no wrapper for DF12's
    # ``trivial-attribute-wrapper`` to reject.
    wiring.admission.cache_allowed = watch.cache_allowed
    recovery = cache_recovery.CacheRecovery(
        settings.cache_recovery,
        server,
        settings.recovery_marker,
        counts=wiring.admission.counts,
        # Read live: recovery asks on every step, not once per fault.
        fault_kind=lambda: watch.drain_fault_kind,
        zero_visible_roots=lambda: watch.zero_visible_roots,
        roots_are_visible=lambda: watch.roots_are_visible,
        cache_mode=settings.cache_mode,
        metrics=wiring.metrics,
        hold_cache=watch.hold_cache,
        release_cache=watch.release_cache,
    )
    holder.append(recovery)
    return watch, recovery
