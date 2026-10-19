"""Feed OS events to Admission on one selector thread; pool reads use another."""

import contextlib
import errno
import selectors
import time
import typing as typ

from buildlimits import (
    cache_recovery,
    cgroups,
    config,
    effect_dispatch,
    group_recovery,
    startup,
    status,
    supervisor_observability,
    supervisor_polling,
    supervisor_shutdown,
    supervisor_signals,
)
from buildlimits.admission import Admission, GroupState
from buildlimits.drain_loop import MAX_INTERNAL_FAILURES as MAX_INTERNAL_FAILURES
from buildlimits.drain_loop import DrainLoop, LoopEffects
from buildlimits.groups import Groups
from buildlimits.journal import admitted_line, log
from buildlimits.metrics import Metrics, gauge_fields
from buildlimits.pool import PoolFailure, Token, TokenPool
from buildlimits.protocol import Admitted
from buildlimits.requesters import Requests, listen
from buildlimits.sccache import ManagedSccache
from buildlimits.server_watch import ServerWatch
from buildlimits.service_readiness import notify_ready
from buildlimits.systemd import UserManager
from buildlimits.timing import Clock, as_clock

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import socket
    from pathlib import PurePosixPath

    from buildlimits.admission import Effect
    from buildlimits.metrics import MetricsPort


TICK_SECONDS = 0.1


class Supervisor:
    """The running supervisor.

    Parameters
    ----------
    settings : config.SupervisorConfig
        The validated configuration.
    manager : UserManager
        The user manager port.
    clock : Callable[[], float] | Clock
        Monotonic time; a Clock also supplies control-fault retry sleep.
    metrics : MetricsPort | None
        Failure-isolated emitter; defaults to a no-op transport.
    """

    def __init__(
        self,
        settings: config.SupervisorConfig,
        manager: UserManager | None = None,
        clock: cabc.Callable[[], float] | Clock = time.monotonic,
        *,
        metrics: MetricsPort | None = None,
    ) -> None:
        """Assemble the supervisor; :meth:`start` does the work of starting."""
        self.settings = settings
        self.manager = manager or UserManager()
        timing = as_clock(clock)
        self.timing = timing
        self.clock = timing.now
        self.metrics = metrics if metrics is not None else Metrics()

        self.admission = Admission(cached=False, capacity=settings.slots)
        self.selector = selectors.DefaultSelector()
        self.requests = Requests(
            self.selector,
            self.clock,
            metrics=self.metrics,
            counts=self.admission.counts,
        )
        self.groups = Groups(settings, self.manager, self.selector, self.clock)
        server = ManagedSccache(
            settings.sccache_unit, settings.sccache_socket, self.manager
        )
        self.server = ServerWatch(
            server,
            self.admission,
            settings.cache_mode,
            clock=self.clock,
            metrics=self.metrics,
            # Read live, and resolved late: the recovery machine is built from
            # this watch's readings, so it cannot be named until both exist.
            automatic_recovery_pending=lambda: bool(
                self.recovery.describe(self.clock())["pending"]
            ),
        )
        # Installed once the watch exists, because the permission it answers
        # for cannot be read before then. A bound method rather than a closure
        # or a forwarding method: the reference is resolved per call, so the
        # watch's later decisions are the ones that count, with no wrapper for
        # DF12's ``trivial-attribute-wrapper`` to reject.
        self.admission.cache_allowed = self.server.cache_allowed
        self.recovery = cache_recovery.CacheRecovery(
            settings.cache_recovery,
            server,
            settings.recovery_marker,
            counts=self.admission.counts,
            # Read live: recovery asks on every step, not once per fault.
            fault_kind=lambda: self.server.drain_fault_kind,
            zero_visible_roots=lambda: self.server.zero_visible_roots,
            roots_are_visible=lambda: self.server.roots_are_visible,
            cache_mode=settings.cache_mode,
            metrics=self.metrics,
            hold_cache=self.server.hold_cache,
            release_cache=self.server.release_cache,
        )
        self.pool: TokenPool | None = None
        self.listener: socket.socket | None = None
        self.exit_status: int | None = None
        self._stopping = False
        self._stop_requested = False
        self._graceful_stop_requested = False
        self._signals_read = -1
        self._signals_write = -1
        self._signals_previous: tuple[int, object] = (-1, None)  # restored on close
        self.loop = DrainLoop(
            sleep=timing.sleep,
            failure_observer=lambda failures: self._metric(
                "internal_error", failures=failures
            ),
        )
        self.health_reason: str | None = None
        self._next_server_poll = 0.0
        self._next_health_poll = 0.0
        self._next_cache_poll = 0.0
        self.pressure = supervisor_observability.configure(self)
        self._effect_handlers = effect_dispatch.effect_handlers(self)

    def start(self, own_cgroup: PurePosixPath) -> None:
        """Pass the restart barrier, open the pool and start listening.

        Parameters
        ----------
        own_cgroup : PurePosixPath
            The supervisor's own control-group path.
        """
        root = startup.user_manager_root(own_cgroup)
        slice_dir = cgroups.directory(
            startup.slice_path(root, self.settings.compilers_slice)
        )
        survivors = startup.drain_slice(slice_dir, self.settings.drain_timeout_seconds)
        log(f"restart barrier passed; terminated {survivors} surviving scope(s)")
        self.groups.slice_dir = slice_dir
        self.server.quiesce_live_cache(self.recovery.barrier, self.timing)
        self.server.drain_orphans()
        self.recovery.reclaim_after_accounting()
        self.pool = TokenPool(self.settings.device)
        self.selector.register(self.pool.wake_fd, selectors.EVENT_READ, "pool")
        self.listener = listen(self.settings.socket_path)
        self.selector.register(self.listener, selectors.EVENT_READ, "listener")
        self._watch_signals()
        self.server.refresh_cache_mode()
        # Only the mode that owns the managed cache may run a recovery.
        if self.settings.cache_mode == "sccache":
            self.requests.recovery = lambda: self.recovery.request(
                "manual", self.clock()
            )
        self.requests.descriptors.configure(slots=self.settings.slots)
        log(
            f"admitting up to {self.settings.slots} compiler group(s) from "
            f"{self.settings.device}; cache mode {self.settings.cache_mode}"
        )
        notify_ready()
        self._metric("supervisor_started", health="healthy")

    @property
    def _failures(self) -> int:
        """Count loop faults without authorizing an early token-pool close."""
        return self.loop.failures

    def serve(self) -> int:
        """Drain existing work before allowing the supervisor process to exit.

        Returns
        -------
        int
            Non-zero after unhealthy recovery; zero after orderly shutdown.
        """
        effects = LoopEffects(
            self._dispatch, self._tick, self._unhealthy, lambda: self.exit_status
        )
        result = self.loop.run(self.selector, effects, tick_seconds=TICK_SECONDS)
        supervisor_shutdown.settle_token_request(self)
        self.requests.close_all()
        return result

    def _dispatch(self, tag: object) -> None:
        """Route one ready descriptor to its handler."""
        supervisor_shutdown.honour_stop_request(self)
        match tag:
            case "listener":
                self._accept()
            case "pool":
                self._pool_events()
            case ("conn", int() as request_id):
                self._connection_ready(request_id)
            case ("errors", str() as group_id):
                self._exec_outcome(group_id)
            case supervisor_signals.SIGNALS_TAG:
                self._reap_leaders()

    def _accept(self) -> None:
        """Accept one connection and queue its compile request.

        Raises
        ------
        OSError
            If acceptance fails for a reason other than temporary pressure.
        """
        if self.listener is None:
            return
        try:
            entry = self.requests.accept(self.listener, self.status)
        except OSError as error:
            if error.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                return
            if error.errno not in {errno.EMFILE, errno.ENFILE}:
                raise
            self.pressure.pause(
                self.listener,
                error.errno,
                self.requests.descriptors,
                self.requests.retained,
            )
            return
        if entry is not None:
            effects = self.admission.submit(
                entry.request_id, cacheable=entry.request.cacheable
            )
            if entry.request_id in self.admission.queue:
                self._metric("request_queued")
            self._apply(effects)

    def _failed_replies(self) -> None:
        """Cancel disconnected requesters before any queued child can launch."""
        while self.requests.failed_replies:
            for request_id in tuple(self.requests.failed_replies):
                self._cancel_pending(request_id)
                self.requests.failed_replies.discard(request_id)

    def _connection_ready(self, request_id: int) -> None:
        """Cancel a request whose requester has gone away."""
        entry = self.requests.disconnected(request_id)
        if entry is None:
            return
        self._cancel_pending(request_id)

    def _cancel_pending(self, request_id: int) -> None:
        """Cancel admission before forgetting an ungrouped recorded requester."""
        entry = self.requests.pending.get(request_id)
        if entry is None:
            return
        self._apply(self.admission.cancel(request_id))
        if entry.group_id is None:
            self.requests.cancel_queued(request_id)

    def _pool_events(self) -> None:
        """Feed tokens and pool failures into admission."""
        self._failed_replies()
        events = () if self.pool is None else self.pool.events()
        for event in events:
            match event:
                case Token(value=value):
                    self._apply(self.admission.token_arrived(value))
                case PoolFailure(reason=reason):
                    self._metric("pool_failure", operation="read")
                    self._unhealthy(reason)

    def _exec_outcome(self, group_id: str) -> None:
        """Record why a released child could not execute its compiler."""
        outcome = self.groups.exec_outcome(group_id)
        if outcome is None:
            return
        request_id, message = outcome
        entry = self.requests.pending.get(request_id)
        if entry is not None:
            entry.exec_error = message

    def _watch_signals(self) -> None:
        """Wake the event loop on SIGCHLD through a self-pipe."""
        supervisor_signals.watch_signals(self)

    _reap_leaders = supervisor_signals.reap_leaders

    # The public stop API and the loop's shutdown seam are bound here rather
    # than defined, so the policy they share lives in ``supervisor_shutdown``
    # beside its sibling ``supervisor_polling``. A bound method keeps the
    # name, the signature and the ``self`` binding every caller already uses.
    request_stop = supervisor_shutdown.request_stop
    request_graceful_stop = supervisor_shutdown.request_graceful_stop
    stop = supervisor_shutdown.stop
    _shutdown = supervisor_shutdown.shutdown
    close = supervisor_shutdown.close

    def _tick(self) -> None:
        """Advance draining groups, escalations and periodic checks."""
        supervisor_shutdown.honour_stop_request(self)
        supervisor_shutdown.settle_graceful_stop(self)
        self.pressure.resume(self.listener)
        self._failed_replies()
        if self._signals_read >= 0:
            self._reap_leaders()
        self.requests.prune_completed(self.admission.groups)
        draining = {
            group.group_id
            for group in self.admission.groups.values()
            if group.state is GroupState.DRAINING
        }
        group_recovery.retry_termination(self.groups, self.admission)
        for group_id in self.groups.advance(draining):
            self.groups.remove(group_id)
            self._apply(self.admission.group_emptied(group_id))
        supervisor_polling.poll_services(self, self.clock())
        if not self.admission.healthy and not self.admission.groups:
            self._shutdown()

    def _unhealthy(self, reason: str) -> None:
        """Stop admitting and drain everything after a pool failure."""
        self.health_reason = self.health_reason or reason
        if not self.admission.healthy:
            return
        effect_dispatch.mark_health_cancellations(self)
        effects = self.admission.health_failed()
        with contextlib.suppress(OSError):
            log(f"unhealthy: {reason}; draining before exit")
        self._metric("health_changed", health="unhealthy")
        self._apply(effects)

    def _apply(self, effects: list[Effect]) -> None:
        """Carry out admission effects in order."""
        for effect in effects:
            self._effect_handlers[type(effect)](effect)

    def _request_token(self) -> None:
        """Ask the pool's reader thread for a token."""
        if self.pool is not None:
            self.pool.request()

    def _return_token(self, token: bytes) -> None:
        """Write a token back, treating failure as loss of pool health."""
        if self.pool is None:
            return
        try:
            self.pool.give_back(token)
        except OSError as error:
            self._metric("pool_failure", operation="return")
            self._unhealthy(f"returning a token failed: {error}")

    def _launch(self, request_id: int, group_id: str, *, cached: bool) -> None:
        """Start an admitted request's compiler inside a new scope."""
        entry = self.requests.pending[request_id]
        running = self.groups.place(entry, group_id, cached=cached)
        if running is None:
            supervisor_shutdown.fail_placement(self, entry, group_id)
            return
        # The requester learns of admission before the compiler can write.
        notice = self.groups.cache_notice(entry, cached=cached)
        effect_dispatch.record_admitted(self, entry, cached=cached)
        self.requests.notify(request_id, Admitted(group_id, cached, notice))
        if entry.conn is None:
            self.groups.abandon_held(running)
            entry.exec_error = running.exec_error
            self._apply(self.admission.launch_failed(group_id))
            return
        if self.groups.release(running) is False:
            entry.exec_error = running.exec_error
            self._apply(self.admission.launch_failed(group_id))
            return
        log(admitted_line(entry, cached=cached))

    def _metric(self, event: str, **fields: object) -> None:
        """Attach the current gauge snapshot to one dispatch observation."""
        self.metrics.emit(event, **gauge_fields(self.admission.counts()), **fields)

    def status(self) -> dict[str, object]:
        """Describe the supervisor's current state.

        Returns
        -------
        dict[str, object]
            A JSON-compatible status document; see :func:`status.describe`.
        """
        return status.describe(self)
