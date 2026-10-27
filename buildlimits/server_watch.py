"""The supervisor's view of the configured cache backend.

The supervisor asks three things of a detached cache: whether a crashed
predecessor left compilations running in it, how many it is running while
server-drain tokens wait, and whether new groups can be composed with it.
:class:`ServerWatch` answers them through the detached adapter -- at present
:class:`buildlimits.sccache.ManagedSccache` -- and turns a cache whose state
cannot be read into the safe decision for each question: start-up fails, the
tokens stay held, and new groups compile directly.

A cache that keeps its work inside the compiler's scope has no detached work
to account for, so nothing waits and nothing is drained. Only one question is
left, whether new groups can be composed with it, and that is asked of
whichever backend the mode selected.

``server`` is therefore the detached adapter and is touched only under
:func:`buildlimits.cache_modes.is_detached_mode`, while ``backend`` is the
health port every cache mode has.
"""

import dataclasses as dc
import time
import typing as typ

from buildlimits import cache_quiesce, server_drain
from buildlimits.cache_backend import CacheBackend, CacheError, DirectCache
from buildlimits.cache_modes import (
    is_cache,
    is_detached_mode,
    mode_name,
    mode_unavailable,
)
from buildlimits.drain_metrics import DrainMetrics
from buildlimits.journal import log
from buildlimits.metrics import Metrics, gauge_fields
from buildlimits.sccache import StartupAccountingError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.admission import Admission
    from buildlimits.drain_guidance import FaultKind
    from buildlimits.metrics import MetricsPort
    from buildlimits.recovery_barrier import RecoveryBarrier
    from buildlimits.sccache import ManagedSccache
    from buildlimits.timing import Clock


@dc.dataclass(slots=True)
class ServerWatch:
    """Probe the managed server on the supervisor's behalf.

    Parameters
    ----------
    server : ManagedSccache
        The managed server. Only a detached mode touches it.
    admission : Admission
        The admission state whose cache mode and server-drain tokens the
        server's state decides.
    cache_mode : str
        The configured cache mode. Only a detached mode needs the drain
        questions answered; every cache mode asks whether it can compose.
    backend : CacheBackend
        The health port of the backend the mode selected. It is read for
        composition permission in every cache mode, including the in-scope
        ones that have no managed server.
    metrics : MetricsPort
        Failure-isolated port for cache and server-drain transitions.
    clock : Callable[[], float]
        Monotonic clock bounding drain-query retries.
    automatic_recovery_pending : Callable[[], bool]
        Reports whether the supervisor's own recovery is due or under way, so a
        live-work fault is not presented as needing an operator while it is.
    drain_metrics : DrainMetrics
        Shared operation observer; never changes cache-accounting authority.
    """

    server: ManagedSccache
    admission: Admission
    cache_mode: str
    backend: CacheBackend = dc.field(default_factory=DirectCache)
    metrics: MetricsPort = dc.field(default_factory=Metrics)
    _fault: str | None = None
    _reachable: bool | None = None
    _load_unknown: bool = False
    clock: cabc.Callable[[], float] = time.monotonic
    drain_fault: str | None = None
    drain_fault_kind: FaultKind | None = None
    drain_attempts: int = 0
    automatic_recovery_pending: cabc.Callable[[], bool] = lambda: False
    _action_kind: FaultKind | None = None
    _journaled_action: str | None = None
    _next_drain: float = 0.0
    _fault_roots: int | None = None
    drain_metrics: DrainMetrics = dc.field(default_factory=DrainMetrics)
    _cache_hold_until: float = 0.0

    def hold_cache(self, seconds: float) -> None:
        """Deny cache composition for a bounded interval from now.

        This is a denial deadline, never evidence: it cannot release a token,
        ``accounted_drain`` remains the sole authority for a server-drain
        token, and every other reading comes from a real probe. It buys the
        interval between asking the cache to stop and observing it back in
        service, in which a reachability reading would be stale.

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
        until the loop's own cache poll observes it answering. Taking the
        reading here rather than deferring it is what closes that window: the
        poll cadence is seconds wide, and the whole stop-and-restart can
        complete inside one interval.
        """
        self._cache_hold_until = 0.0
        self.refresh_cache_mode()

    def cache_allowed(self) -> bool:
        """Report whether cache composition is permitted right now.

        Only a recovery transaction takes a hold, and only a detached backend
        has one to recover, so the deadline answers for every mode: it is never
        moved in a mode that has no transaction. What differs is the mode gate,
        which asks whether a cache was configured at all rather than whether it
        is detached -- an in-scope cache composes while its health holds.

        Returns
        -------
        bool
            Whether the configuration names a cache and no recovery hold is
            outstanding.
        """
        return is_cache(self.cache_mode) and self.clock() >= self._cache_hold_until

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
        if is_detached_mode(self.cache_mode):
            cache_quiesce.quiesce_and_report(
                self.server, barrier, timing, self._report_quiesce
            )

    def _report_quiesce(self, outcome: str, seconds: float) -> None:
        """Meter how a start-up stop of a live cache ended."""
        self.metrics.emit(
            "cache_quiesce", quiesce_outcome=outcome, quiesce_seconds=seconds
        )

    def drain_orphans(self) -> None:
        """Require accounted server work before the admission pool opens.

        Raises
        ------
        StartupAccountingError
            If an earlier server remains live or its stopped state cannot be
            verified. Coordinate a quiet server stop before restarting this
            supervisor; startup must not interrupt another build's work.
        """
        if not is_detached_mode(self.cache_mode):
            return
        try:
            self.server.accounted_drain()
        except CacheError as error:
            name = mode_name(self.cache_mode)
            msg = (
                f"startup cannot account for managed cache work: {error}; "
                f"operator action: coordinate a quiet {name} stop, verify its "
                "empty cgroup, then restart admission; "
                "do not interrupt active builds"
            )
            raise StartupAccountingError(msg) from error

    # The drain-token policy and its readings are bound here rather than
    # defined, so the release decision lives in ``server_drain`` beside the
    # guidance it journals. A bound method keeps the signature and the ``self``
    # binding every caller already uses. The annotations are what tell a reader
    # and a type checker these are members of the class, not fields; they add
    # no value, because the bindings above are the ones the methods use.
    drained_tokens: typ.ClassVar = server_drain.drained_tokens
    recovery_action: typ.ClassVar = property(server_drain.recovery_action)
    recovery_action_is_fallback: typ.ClassVar = property(
        server_drain.recovery_action_is_fallback
    )
    zero_visible_roots: typ.ClassVar = property(server_drain.zero_visible_roots)
    roots_are_visible: typ.ClassVar = property(server_drain.roots_are_visible)

    def refresh_cache_mode(self) -> None:
        """Compose new groups with the cache only while it can serve work."""
        if not is_cache(self.cache_mode):
            self.admission.cached = False
            return
        try:
            reachable, fault = self.backend.is_reachable(), None
        except CacheError as error:
            reachable, fault = False, str(error)
        self._announce_reachability(reachable=reachable, fault=fault)
        self._fault = fault
        self.admission.cached = reachable

    def _announce_reachability(self, *, reachable: bool, fault: str | None) -> None:
        """Journal and meter a reachability reading, once per change."""
        if fault is not None and fault != self._fault:
            log(f"{fault}; compiling directly")
        elif fault is None and reachable != self.admission.cached:
            log(_reachability_line(self.cache_mode, reachable=reachable))
        if reachable != self._reachable:
            self.metrics.emit(
                "cache_reachability",
                **gauge_fields(self.admission.counts()),
                reachable=reachable,
            )
            self._reachable = reachable


def _reachability_line(cache_mode: str, *, reachable: bool) -> str:
    """Word a composition change for the configured backend.

    A detached backend is a service this supervisor owns, so the line says so.
    An in-scope backend is not, and calling it a managed server would send the
    operator looking for a unit that does not exist.

    Parameters
    ----------
    cache_mode : str
        The configured cache mode.
    reachable : bool
        The reading the line reports.

    Returns
    -------
    str
        The journal line.

    Examples
    --------
    >>> _reachability_line("sccache", reachable=False)
    'the managed sccache server is unreachable; compiling directly'
    >>> _reachability_line("kache", reachable=False)
    'kache is unavailable; compiling directly'
    >>> _reachability_line("kache", reachable=True)
    'kache is caching compilations'
    """
    name = mode_name(cache_mode)
    state = (
        "caching compilations"
        if reachable
        else f"{mode_unavailable(cache_mode)}; compiling directly"
    )
    if is_detached_mode(cache_mode):
        return f"the managed {name} server is {state}"
    return f"{name} is {state}"
