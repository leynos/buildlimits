"""The supervisor's view of the managed sccache server.

The supervisor asks three things of the managed server: whether a crashed
predecessor left compilations running in it, how many compilations it is
running while server-drain tokens wait, and whether new groups can be composed
with it. :class:`ServerWatch` answers them through
:class:`buildlimits.sccache.ManagedSccache` and turns a server whose state
cannot be read into the safe decision for each question: start-up fails, the
tokens stay held, and new groups compile directly.
"""

import contextlib
import dataclasses as dc
import time
import typing as typ

from buildlimits import cache_quiesce
from buildlimits.admission import GroupState, ReturnToken
from buildlimits.drain_metrics import DrainMetrics
from buildlimits.journal import log
from buildlimits.metrics import Metrics, gauge_fields
from buildlimits.sccache import (
    LiveCacheWorkError,
    SccacheError,
    StartupAccountingError,
    StoppedPopulatedCacheError,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.admission import Admission, Effect
    from buildlimits.metrics import MetricsPort
    from buildlimits.recovery_barrier import RecoveryBarrier
    from buildlimits.sccache import ManagedSccache
    from buildlimits.timing import Clock


DRAIN_FAILURE_LIMIT = 8
DRAIN_RETRY_SECONDS = 30.0

type FaultKind = typ.Literal["live_work", "stopped_populated", "manager"]

# Each fault kind carries one journal prefix and one piece of operator
# guidance, so a new kind cannot silently reuse another's advice.
_FAULT_PREFIXES: dict[FaultKind, str] = {
    "live_work": "holding tokens for ",
    "stopped_populated": "holding tokens while the stopped cache is not empty: ",
    "manager": "cannot read the sccache server's load; holding tokens: ",
}

# Operator guidance is chosen by what the failed check actually proved:
# live work needs a coordinated recovery, a stopped populated service needs
# its cgroup inspected, and a transport fault needs the manager restored.
_RECOVERY_ACTIONS: dict[FaultKind, str] = {
    "live_work": (
        "Run build-limits-recover to stop the managed sccache service under the "
        "admission barrier, verify it is stopped and empty, and release the "
        "retained tokens; tokens remain held until that verification succeeds."
    ),
    "stopped_populated": (
        "The managed sccache service is stopped but its control group still "
        "holds work. Inspect the surviving processes and wait for them to "
        "exit; tokens remain held until the group is empty."
    ),
    "manager": (
        "Restore the systemd user-manager transport and re-read the managed "
        "sccache service state; do not stop or restart sccache for this fault, "
        "and do not release tokens until the service is verified stopped and "
        "empty."
    ),
}


@dc.dataclass(slots=True)
class ServerWatch:
    """Probe the managed server on the supervisor's behalf.

    Parameters
    ----------
    server : ManagedSccache
        The managed server.
    admission : Admission
        The admission state whose cache mode and server-drain tokens the
        server's state decides.
    cache_mode : str
        The configured cache mode; only ``"sccache"`` composes with the server.
    metrics : MetricsPort
        Failure-isolated port for cache and server-drain transitions.
    clock : Callable[[], float]
        Monotonic clock bounding drain-query retries.
    drain_metrics : DrainMetrics
        Shared operation observer; never changes cache-accounting authority.
    """

    server: ManagedSccache
    admission: Admission
    cache_mode: str
    metrics: MetricsPort = dc.field(default_factory=Metrics)
    _fault: str | None = None
    _reachable: bool | None = None
    _load_unknown: bool = False
    clock: cabc.Callable[[], float] = time.monotonic
    drain_fault: str | None = None
    drain_fault_kind: FaultKind | None = None
    drain_attempts: int = 0
    recovery_action: str | None = None
    _next_drain: float = 0.0
    _fault_roots: int | None = None
    drain_metrics: DrainMetrics = dc.field(default_factory=DrainMetrics)
    _cache_hold_until: float = 0.0

    def hold_cache(self, seconds: float) -> None:
        """Deny cache composition for a bounded interval from now.

        This is a denial deadline, never evidence. It cannot release a token
        and it never establishes anything about the server: ``accounted_drain``
        remains the sole authority for a server-drain token, and every reading
        here still comes from a real probe. What the deadline buys is the one
        interval in which the supervisor knows, without any probe, that a
        composition would be wrong — between asking the cache to stop and
        observing it back in service — which is exactly the interval in which a
        reachability reading is stale evidence of a server that is about to go
        away.

        Parameters
        ----------
        seconds : float
            How long composition stays denied. Repeated calls move the only
            deadline, bounded by the caller, so a hold cannot be renewed into
            perpetuity by the machine that is supposed to expose its own
            failure.
        """
        self._cache_hold_until = max(self._cache_hold_until, self.clock() + seconds)

    def release_cache(self) -> None:
        """End the recovery denial and re-decide composition on a fresh reading.

        Only the recovery transaction calls this, and only once its restart has
        been issued. The restart is a queued job rather than an awaited one, so
        issuing it is no evidence that the server is back: the hold is retired
        and immediately replaced by a real reachability probe. A server that is
        still coming up reads as unreachable, and composition stays denied
        until the loop's own cache poll observes it answering — so no group is
        ever composed from the reading taken before the stop. Taking the
        reading here rather than deferring it is what closes that window: the
        poll cadence is seconds wide, and the whole stop-and-restart can
        complete inside one interval.
        """
        self._cache_hold_until = 0.0
        self.refresh_cache_mode()

    def cache_allowed(self) -> bool:
        """Report whether cache composition is permitted right now.

        Returns
        -------
        bool
            Whether the configuration composes with sccache and no recovery
            hold is outstanding.
        """
        return self.cache_mode == "sccache" and self.clock() >= self._cache_hold_until

    def quiesce_live_cache(self, barrier: RecoveryBarrier, timing: Clock) -> None:
        """Stop a live cache under the barrier so that start-up can account for it.

        Parameters
        ----------
        barrier : RecoveryBarrier
            The restart barrier the managed unit's condition reads.
        timing : Clock
            Monotonic time and sleep for the bounded wait.

        Raises
        ------
        StartupAccountingError
            If the live cache cannot be stopped and verified; see
            :func:`buildlimits.cache_quiesce.quiesce_live_cache`.
        """  # ruff: ignore[docstring-extraneous-exception] - cache_quiesce raises it.
        if self.cache_mode == "sccache":
            cache_quiesce.quiesce_live_cache(self.server, barrier, timing)

    def drain_orphans(self) -> None:
        """Require accounted server work before the admission pool opens.

        Raises
        ------
        StartupAccountingError
            If an earlier server remains live or its stopped state cannot be
            verified. Coordinate a quiet server stop before restarting this
            supervisor; startup must not interrupt another build's work.
        """
        if self.cache_mode != "sccache":
            return
        try:
            self.server.accounted_drain()
        except SccacheError as error:
            msg = (
                f"startup cannot account for managed cache work: {error}; "
                "operator action: coordinate a quiet sccache stop, verify its "
                "empty cgroup, then restart admission; do not interrupt active builds"
            )
            raise StartupAccountingError(msg) from error

    def drained_tokens(self) -> list[Effect]:
        """Decide which server-drain tokens the server's load releases.

        Returns
        -------
        list[Effect]
            The returns admission decides on, or nothing while no token waits
            or the load cannot be read.
        """
        waiting = any(
            group.state is GroupState.SERVER_DRAIN
            for group in self.admission.groups.values()
        )
        if not waiting:
            self._clear_drain_fault()
            return []
        if self.clock() < self._next_drain:
            return []
        try:
            active = self.drain_metrics.call(
                "accounted_cache", self.server.accounted_drain
            )
        except SccacheError as error:
            self._load_state(unknown=True)
            self._record_drain_fault(error)
            return []
        return self._release_accounted(active)

    def _release_accounted(self, active: int) -> list[Effect]:
        """Release what a verified stopped-and-empty cache leaves unaccounted.

        Only reached once the manager has proved the cache stopped and empty,
        so this is where the earlier fault is retired and the tokens the server
        is no longer covering go back. The reading is not re-derived here: the
        composition flag is whatever :meth:`refresh_cache_mode` last decided
        from a real reachability probe, and this method changes no part of it.

        Returns
        -------
        list[Effect]
            The returns admission decides on for ``active`` live compilations.
        """
        if self.drain_fault is not None:
            with contextlib.suppress(OSError):
                log(
                    "sccache service verified stopped and empty; "
                    "drain tokens may return"
                )
        self._clear_drain_fault()
        self._load_state(unknown=False)
        effects = self.admission.server_activity(active)
        released = sum(isinstance(effect, ReturnToken) for effect in effects)
        if released:
            self.metrics.emit(
                "server_drain_released",
                **gauge_fields(self.admission.counts()),
                released=released,
            )
        return effects

    def _record_drain_fault(self, error: SccacheError) -> None:
        """Keep unknown work accounted for and deduplicate drain diagnostics."""
        kind = _fault_kind(error)
        retained = kind == "live_work"
        detail = ascii(str(error)[:400])[1:-1]
        fault = f"retained detached work: {detail}" if retained else detail
        self.drain_attempts += 1
        changed = fault != self.drain_fault or kind != self.drain_fault_kind
        self.drain_fault = fault
        self.drain_fault_kind = kind
        is_live = isinstance(error, LiveCacheWorkError)
        self._fault_roots = error.roots if is_live else None
        if changed:
            with contextlib.suppress(OSError):
                log(f"{_FAULT_PREFIXES[kind]}{fault}")
        if self.drain_attempts >= DRAIN_FAILURE_LIMIT:
            self._next_drain = self.clock() + DRAIN_RETRY_SECONDS
            if self.recovery_action != _RECOVERY_ACTIONS[kind]:
                self.recovery_action = _RECOVERY_ACTIONS[kind]
                with contextlib.suppress(OSError):
                    log(f"drain requires operator action: {self.recovery_action}")

    @property
    def zero_visible_roots(self) -> bool:
        """Whether the current live-work fault proved zero visible compiler roots.

        Returns
        -------
        bool
            True only for a live-work fault whose root count was established
            as zero. An unknown count and a manager fault both answer False,
            so the automatic trigger cannot act on an unproven reading.
        """
        return self.drain_fault_kind == "live_work" and self._fault_roots == 0

    @property
    def roots_are_visible(self) -> bool:
        """Whether the current live-work fault established a visible root.

        Returns
        -------
        bool
            True only for a live-work fault whose root count was established
            as positive; an unknown count answers False, as it does for the
            zero-root proof, so neither proof is ever guessed.
        """
        roots = self._fault_roots
        return self.drain_fault_kind == "live_work" and roots is not None and roots > 0

    def _clear_drain_fault(self) -> None:
        """End the query fault only after a successful check or no waiting work."""
        self.drain_fault = None
        self.drain_fault_kind = None
        self.drain_attempts = 0
        self.recovery_action = None
        self._next_drain = 0.0
        self._fault_roots = None

    def refresh_cache_mode(self) -> None:
        """Compose new groups with sccache only while the server is reachable."""
        if self.cache_mode != "sccache":
            self.admission.cached = False
            return
        try:
            reachable, fault = self.server.is_reachable(), None
        except SccacheError as error:
            reachable, fault = False, str(error)
        self._announce_reachability(reachable=reachable, fault=fault)
        self._fault = fault
        self.admission.cached = reachable

    def _announce_reachability(self, *, reachable: bool, fault: str | None) -> None:
        """Journal and meter a reachability reading, once per change."""
        if fault is not None and fault != self._fault:
            log(f"{fault}; compiling directly")
        elif fault is None and reachable != self.admission.cached:
            log(
                "the managed sccache server is reachable; caching compilations"
                if reachable
                else "the managed sccache server is unreachable; compiling directly"
            )
        if reachable != self._reachable:
            self.metrics.emit(
                "cache_reachability",
                **gauge_fields(self.admission.counts()),
                reachable=reachable,
            )
            self._reachable = reachable

    def _load_state(self, *, unknown: bool) -> None:
        """Emit one transition per change in server-load readability."""
        if unknown != self._load_unknown:
            self._load_unknown = unknown
            self.metrics.emit(
                "cache_load_unknown",
                **gauge_fields(self.admission.counts()),
                load_unknown=unknown,
            )


def _fault_kind(error: SccacheError) -> FaultKind:
    """Classify a drain-check failure for guidance and automatic recovery.

    Returns
    -------
    FaultKind
        ``live_work`` for a live service that may hold detached requests,
        ``stopped_populated`` for a stopped service that still contains work,
        or ``manager`` for any fault reading the service state.
    """
    match error:
        case LiveCacheWorkError():
            return "live_work"
        case StoppedPopulatedCacheError():
            return "stopped_populated"
        case _:
            return "manager"
