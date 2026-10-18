"""Recover shared admission when detached cache work retains its tokens.

An interrupted cached compilation can leave accepted work inside the managed
sccache server after its compiler scope has emptied. Admission holds that
token until the server is verified stopped and empty, so a server that retains
detached work starves every other build until an operator intervenes. This
module drives that intervention as a sequence of steps whose calls are
bounded and whose retries are delayed: write the restart barrier, stop the
cache, wait for the accounting, then clear the barrier and start the cache.

Only the two middle steps can fail, and neither releases a token. Time
authorizes one thing only: an automatic stop whose visible roots outlast
``RECOVERY_ROOTS_GRACE_SECONDS`` with nothing cached moving. A refused stop
retries for ever and accounting waits for ever for proof, so only the drain
probe's verified stopped-and-empty state returns a token.
"""

import contextlib
import dataclasses as dc
import typing as typ

from buildlimits import recovery_roots, recovery_starvation
from buildlimits.journal import log
from buildlimits.metrics import Metrics, MetricsPort, gauge_fields
from buildlimits.recovery_guidance import report_exhaustion
from buildlimits.recovery_reclaim import (
    new_barrier,
    reclaim_after_accounting,
    restart_fault,
)
from buildlimits.systemd import SystemdError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.admission_state import Counts
    from buildlimits.recovery_barrier import RecoveryBarrier
    from buildlimits.sccache import ManagedSccache

RECOVERY_RETRY_SECONDS = 30.0
RECOVERY_MAX_RETRY_SECONDS = 120.0
RECOVERY_FAILURE_LIMIT = 8

type Phase = typ.Literal["idle", "quiescing", "stopping", "accounting", "restarting"]


@dc.dataclass(slots=True)
class CacheRecovery:
    """Stop, account for, and restart the managed cache as one transaction.

    Parameters
    ----------
    policy : str
        ``manual`` to act only on operator request, or ``starved`` to also
        open a transaction when a waiting request is blocked by cache work.
    cache : ManagedSccache
        The managed server, whose control verbs queue jobs rather than
        awaiting them.
    marker : Path
        The restart barrier; the managed unit refuses to start while the
        file exists, so no cache returns before its work is accounted.
    counts : Callable[[], Counts]
        Reads the admission gauges; this machine never advances the count.
    fault_kind : Callable[[], str | None]
        Classifies the drain probe's last fault; a manager fault never stops
        or restarts the cache.
    zero_visible_roots : Callable[[], bool]
        Reports whether the drain probe established zero visible compiler
        roots; an unproven reading answers False and releases no token.
    cache_mode : str
        Only ``sccache`` owns the barrier and the managed server.
    metrics : MetricsPort
        Failure-isolated port for recovery transitions.
    hold_cache : Callable[[float], None]
        Denies cache composition for that many seconds, and nothing else, to
        close the window in which a token can arrive between stop and restart.
        It carries no proof and releases nothing.
    release_cache : Callable[[], None]
        Ends the hold once the restart has been accepted, re-deciding
        composition on a fresh probe. A refused restart keeps the hold, because
        a group composed with a server that did not come back is unaccounted.
    """

    policy: str
    cache: ManagedSccache
    marker: Path
    counts: cabc.Callable[[], Counts] = dc.field(kw_only=True)
    fault_kind: cabc.Callable[[], str | None] = dc.field(kw_only=True)
    zero_visible_roots: cabc.Callable[[], bool] = dc.field(
        default=lambda: False, kw_only=True
    )
    cache_mode: str = dc.field(default="sccache", kw_only=True)
    metrics: MetricsPort = dc.field(default_factory=Metrics, kw_only=True)
    hold_cache: cabc.Callable[[float], None] = dc.field(
        default=lambda _seconds: None, kw_only=True
    )
    release_cache: cabc.Callable[[], None] = dc.field(
        default=lambda: None, kw_only=True
    )
    # The transaction's own state is never a constructor argument: keeping it
    # out of ``__init__`` leaves the port above as the whole public shape, so a
    # caller cannot pass a phase where the managed server belongs.
    _phase: Phase = dc.field(default="idle", init=False)
    _attempts: int = dc.field(default=0, init=False)
    _next_action: float = dc.field(default=0.0, init=False)
    _last_fault: str | None = dc.field(default=None, init=False)
    _cooling: float = dc.field(default=0.0, init=False)
    _trigger: str = dc.field(default="manual", init=False)
    _drain_age: recovery_starvation.RetainedDrainAge = dc.field(
        default_factory=recovery_starvation.RetainedDrainAge, init=False
    )
    _roots_grace: recovery_roots.VisibleRootsGrace = dc.field(
        default_factory=recovery_roots.VisibleRootsGrace, init=False
    )
    # Derived from ``marker`` rather than passed, so every caller supplies the
    # one fact the managed unit's own condition also reads: the path.
    barrier: RecoveryBarrier = dc.field(init=False)

    def __post_init__(self) -> None:
        """Derive the barrier handle from the marker path."""
        self.barrier = new_barrier(self.marker)

    def describe(self, now: float) -> dict[str, object]:
        """Describe the transaction for the status document.

        Parameters
        ----------
        now : float
            Current monotonic time.

        Returns
        -------
        dict[str, object]
            The phase, the consecutive failures of its last action, the last
            fault text, the seconds until the next attempt, and the seconds
            before the automatic trigger may open another transaction, and the
            seconds visible roots have delayed an automatic stop.
        """
        return {
            "phase": self._phase,
            "attempts": self._attempts,
            "last_fault": self._last_fault,
            "retry_in": round(max(0.0, self._next_action - now), 3),
            "cooling_for": round(max(0.0, self._cooling - now), 3),
            "roots_waited": round(self._roots_grace.elapsed(now), 3),
        }

    def request(self, trigger: str, now: float) -> dict[str, object]:
        """Open one transaction, or report the running one.

        Parameters
        ----------
        trigger : str
            ``starved`` for the automatic policy, ``manual`` for an operator.
        now : float
            Current monotonic time.

        Returns
        -------
        dict[str, object]
            The transaction's description plus ``blocked``: empty when it is
            running, otherwise why it must not start.
        """
        if self._phase != "idle":
            return self.describe(now) | {"blocked": ""}
        reason = recovery_starvation.refusal(self.fault_kind())
        if not reason:
            # Held now as well as on the first tick: a group composed between
            # the two would be running when the stop is queued.
            self.hold_cache(RECOVERY_MAX_RETRY_SECONDS)
            self._begin(trigger, now)
        return self.describe(now) | {"blocked": reason}

    def tick(self, now: float) -> None:
        """Advance at most one step of the transaction.

        Parameters
        ----------
        now : float
            Current monotonic time; the machine holds no clock of its own.
        """
        if self._phase == "idle":
            self._starved(now)
            return
        # Renewed on every tick, backoff ticks included, so the window cannot
        # reopen mid-retry; the bound matches the retry delay's.
        self.hold_cache(RECOVERY_MAX_RETRY_SECONDS)
        if now >= self._next_action:
            self._step(now)

    def request_stop(self) -> None:
        """Ask the manager to stop the managed cache under the barrier.

        Raises
        ------
        SystemdError
            If the manager refuses the job, cannot be reached, or last failed
            a drain query for transport reasons. A cache whose state cannot be
            read must not be stopped: the stop would be a guess, and the
            manager that is meant to perform it is the part that is broken.
        """
        if self.fault_kind() == "manager":
            msg = (
                f"cannot stop {self.cache.unit}: the user manager transport is "
                "faulted; restore it and the cache's state is readable again"
            )
            raise SystemdError(msg)
        self.cache.stop()

    def reclaim_after_accounting(self) -> None:
        """Retire the barrier a prior, interrupted transaction left behind.

        The work is :func:`buildlimits.recovery_reclaim.reclaim_after_accounting`;
        this supplies the barrier and cache the machine already holds.
        """
        reclaim_after_accounting(
            cache_mode=self.cache_mode, barrier=self.barrier, cache=self.cache
        )

    def _starved(self, now: float) -> None:
        """Open the automatic transaction only for live-cache starvation."""
        counts = self.counts()
        reading = recovery_starvation.StarvationReading(
            cache_mode=self.cache_mode,
            policy=self.policy,
            cooling=self._cooling,
            now=now,
            counts=counts,
            fault_kind=self.fault_kind(),
            zero_visible_roots=self.zero_visible_roots(),
            drain_age=self._drain_age.observe(counts.server_drain, now=now),
        )
        reason = recovery_starvation.starvation(reading)
        if reason is not None:
            self._begin("starved", now, reason)
            if recovery_starvation.proof(reading) == "stale":
                self.metrics.emit(
                    "cache_recovery", phase=self._phase, recovery_stale_openings=1
                )

    def _begin(self, trigger: str, now: float, reason: str = "") -> None:
        """Open one transaction and announce it once, with the reason if known."""
        self._trigger = trigger
        self._roots_grace.reset()
        self._advance("quiescing", now)
        with contextlib.suppress(OSError):
            why = f" ({reason})" if reason else ""
            log(
                f"{trigger} recovery of the managed cache{why}: writing the "
                f"restart barrier {self.barrier.path}"
            )

    def _step(self, now: float) -> None:
        """Carry out the current phase's due action by phase name."""
        match self._phase:
            case "quiescing":
                self._write_barrier(now)
            case "stopping":
                self._try_stop(now)
            case "accounting":
                self._account(now)
            case "restarting":
                self._try_start(now)

    def _write_barrier(self, now: float) -> None:
        """Write the barrier before asking the cache to stop."""
        try:
            self.barrier.write()
        except OSError as error:
            self._fail(now, f"cannot write {self.barrier.path}: {error}")
            return
        self._advance("stopping", now)

    def _try_stop(self, now: float) -> None:
        """Queue the stop, retrying without bound: abandoning strands the barrier."""
        # Keeps the stop from killing a cached compile; groups the hold
        # composed without the cache do not count.
        if reason := recovery_starvation.blocked(self.counts(), self.fault_kind()):
            self._roots_grace.reset()
            self._fail(now, reason)
            return
        # A manual stop is exempt: its evidence is the operator's own.
        verdict = recovery_roots.stop_verdict(
            self._roots_grace,
            is_automatic=self._trigger == "starved",
            zero_visible_roots=self.zero_visible_roots(),
            now=now,
        )
        if verdict == "waiting":
            self._fail(now, "the visible compiler roots proof lapsed before stopping")
            return
        try:
            self.request_stop()
        except SystemdError as error:
            self._fail(now, str(error))
            return
        if verdict == "deadline":
            self._note_deadline_stop(now)
        self._advance("accounting", now)

    def _note_deadline_stop(self, now: float) -> None:
        """Journal and count a stop taken on the roots deadline."""
        with contextlib.suppress(OSError):
            log(recovery_roots.deadline_stop_message(self._roots_grace, now))
        self.metrics.emit(
            "cache_recovery", phase=self._phase, recovery_roots_deadline_stops=1
        )

    def _account(self, now: float) -> None:
        """Wait until no token still waits on the cache's accounting.

        The count falls only when the drain probe has verified a stopped and
        empty service and released the token it held, so this phase adds no
        evidence of its own and can only wait.
        """
        self._next_action = now
        server_drain = self.counts().server_drain
        # Retire the age here: a new drain may land before the next idle tick.
        self._drain_age.observe(server_drain, now=now)
        if server_drain == 0:
            self._advance("restarting", now)

    def _try_start(self, now: float) -> None:
        """Clear the barrier, restart the cache, and give up after the limit.

        A start queued behind a barrier that is still there reaches a unit
        whose own condition skips it, so a barrier that cannot be removed is a
        failed step like any other: it is retried with backoff, and the
        operator is told to restore the runtime directory the barrier lives in.
        """
        # Released only once the start is accepted: a refused start leaves the
        # server stopped, and a compiler composed then would start an
        # unaccountable server of its own.
        try:
            self.barrier.clear()
            self.cache.start()
        except (OSError, SystemdError) as error:
            self._fail(now, restart_fault(error, self.barrier))
            if self._attempts >= RECOVERY_FAILURE_LIMIT:
                self.metrics.emit(
                    "cache_recovery", phase=self._phase, recovery_failures=1
                )
                self._finish(now)
            return
        self.release_cache()
        # Retire earlier attempts' faults: an idle fault means the end failed.
        self._last_fault = None
        self._finish(now)

    def _advance(self, phase: Phase, now: float) -> None:
        """Record one successful action and move to the next phase."""
        self._phase = phase
        self._attempts = 0
        self._last_fault = None
        self._next_action = now
        self.metrics.emit("cache_recovery", **gauge_fields(self.counts()), phase=phase)

    def _finish(self, now: float) -> None:
        """End the transaction, leaving the cooldown before another may start.

        A restart that could not be issued still ends the transaction: its
        accounting is complete and its tokens have already returned, so
        withholding progress further would only hide the fault.

        ``recovery_completions`` counts the transactions that ended; the
        faults taken on the way there are counted at the checkpoint below, so
        a coherent recovery and a faulted one are told apart by whether a
        ``recovery_failures`` record shares its transaction.
        """
        self._phase = "idle"
        self._attempts = 0
        self._next_action = 0.0
        self._roots_grace.reset()
        faulted = self._last_fault is not None
        self._cooling = now + recovery_starvation.cooldown_after(faulted=faulted)
        self.metrics.emit(
            "cache_recovery",
            **gauge_fields(self.counts()),
            phase=self._phase,
            recovery_completions=1,
        )

    def _fail(self, now: float, fault: str) -> None:
        """Record one failed action, back off, and keep the fault visible."""
        self._attempts += 1
        detail = ascii(fault[:400])[1:-1]
        changed = detail != self._last_fault
        self._last_fault = detail
        self._next_action = now + min(
            RECOVERY_RETRY_SECONDS * self._attempts, RECOVERY_MAX_RETRY_SECONDS
        )
        # Counted per failure, not per distinct message; only the journal line
        # is deduplicated.
        self.metrics.emit("cache_recovery", phase=self._phase, recovery_attempts=1)
        if changed:
            with contextlib.suppress(OSError):
                log(f"cache recovery: {detail}")
        if self._attempts == RECOVERY_FAILURE_LIMIT:
            report_exhaustion(self._phase, self.metrics)
