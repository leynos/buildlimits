"""Recover shared admission when detached cache work retains its tokens.

An interrupted cached compilation can leave accepted work inside the managed
sccache server after its compiler scope has emptied. Admission holds that
token until the server is verified stopped and empty, so a server that retains
detached work starves every other build until an operator intervenes. This
module drives that intervention as a sequence of steps whose calls are
bounded and whose retries are delayed: write the restart barrier, stop the
cache, wait for the accounting, then clear the barrier and start the cache.

Only the two middle steps can fail, and neither releases a token. This is
not a deadline: a refused stop retries for ever and accounting waits for
ever for proof, so only the drain probe's verified stopped-and-empty state
returns a token; time, idle CPU or visible processes never do.
"""

import contextlib
import dataclasses as dc
import typing as typ

from buildlimits.journal import log
from buildlimits.metrics import Metrics, MetricsPort, gauge_fields
from buildlimits.recovery_guidance import report_exhaustion
from buildlimits.recovery_reclaim import (
    new_barrier,
    reclaim_after_accounting,
    restart_fault,
)
from buildlimits.recovery_starvation import StarvationReading, blocked, starvation
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
# The cooldown outlasts the whole bounded retry sequence (780 s for the values
# above), so a cache that is merely slow to restart cannot exhaust its attempts
# exactly as recovery becomes available again.
RECOVERY_COOLDOWN_SECONDS = 900.0

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
        Reads the admission gauges; the server-drain count is the accounting
        evidence and is never advanced by this machine.
    fault_kind : Callable[[], str | None]
        Classifies the drain probe's last fault, so a manager transport fault
        is never answered by stopping or restarting the cache.
    zero_visible_roots : Callable[[], bool]
        Reports whether the drain probe's live-work fault established zero
        visible compiler roots. Corroborates the automatic trigger only: an
        unproven reading answers False and never releases a token.
    cache_mode : str
        Only ``sccache`` owns the barrier and the managed server.
    metrics : MetricsPort
        Failure-isolated port for recovery transitions.
    hold_cache : Callable[[float], None]
        Denies cache composition for that many seconds, and nothing else. The
        transaction has to close the window in which a token can arrive while
        the server is between stop and restart, and only the admission state
        knows when a token arrives; this port is how the machine asks for that
        window to be closed. It carries no proof and releases nothing.
    release_cache : Callable[[], None]
        Ends the hold once the restart has been accepted, re-deciding
        composition on a fresh probe rather than the pre-stop reading: the
        start is a queued job, so acceptance is not evidence the server is
        back. A refused restart keeps the hold, because composing a group
        with a server that did not come back leaves it unaccounted.
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
            fault text, and the seconds until the next attempt.
        """
        return {
            "phase": self._phase,
            "attempts": self._attempts,
            "last_fault": self._last_fault,
            "retry_in": round(max(0.0, self._next_action - now), 3),
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
        # The two readings are asked for together, at the one point a transaction
        # is decided, so the policy beside the starvation trigger owns the answer.
        reason = blocked(self.counts(), self.fault_kind())
        if not reason:
            # The hold is taken here as well as on the first tick, because a
            # token can arrive between this request and that tick, and a group
            # composed in that gap would be running when the stop is queued.
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
        # Renewed on every tick, including the backoff ticks that take no step,
        # so a retry sequence that has not yet issued its restart cannot let
        # the window reopen underneath a request the supervisor is still
        # deciding to admit. The bound is the same one that bounds the retry
        # delay, so the denial outlasts any single bounded attempt.
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

        The work itself is
        :func:`buildlimits.recovery_reclaim.reclaim_after_accounting`, which
        takes the barrier and cache it needs. This method only supplies the two
        facts the machine already holds, so the supervisor's start-up keeps
        reaching for its recovery handle rather than for a free function it
        would have to be handed the barrier to call.
        """
        reclaim_after_accounting(
            cache_mode=self.cache_mode, barrier=self.barrier, cache=self.cache
        )

    def _starved(self, now: float) -> None:
        """Open the automatic transaction only for starvation by live cache work."""
        reading = StarvationReading(
            cache_mode=self.cache_mode,
            policy=self.policy,
            cooling=self._cooling,
            now=now,
            counts=self.counts(),
            fault_kind=self.fault_kind(),
            zero_visible_roots=self.zero_visible_roots(),
        )
        if starvation(reading, self.barrier) is not None:
            self._begin("starved", now)

    def _begin(self, trigger: str, now: float) -> None:
        """Open one transaction and announce it once."""
        self._trigger = trigger
        self._advance("quiescing", now)
        with contextlib.suppress(OSError):
            log(
                f"{trigger} recovery of the managed cache: writing the restart "
                f"barrier {self.barrier.path}"
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
        # Admission keeps granting tokens while the transaction runs. An
        # automatic transaction holds the cache only from its first tick, so a
        # waiting request can become a cached running group before this stop.
        # The check must be here, not only in ``request``: stopping the cache
        # would kill that compile. Groups the hold composed without the cache
        # do not count, or a busy queue would refuse this stop indefinitely.
        if reason := blocked(self.counts(), self.fault_kind()):
            self._fail(now, reason)
            return
        # The zero-root proof can lapse between the trigger and this stop — a
        # root count is a diagnostic sample, not a held state. It re-arms the
        # transaction instead of authorizing the stop, so an automatic stop is
        # never issued on evidence that has gone stale. A manual request skips
        # this: its evidence is the operator's own, and stalling would withhold
        # the only remediation for a detached-work fault.
        if self._trigger == "starved" and not self.zero_visible_roots():
            self._fail(now, "the visible compiler roots proof lapsed before stopping")
            return
        try:
            self.request_stop()
        except SystemdError as error:
            self._fail(now, str(error))
            return
        self._advance("accounting", now)

    def _account(self, now: float) -> None:
        """Wait until no token still waits on the cache's accounting.

        The count falls only when the drain probe has verified a stopped and
        empty service and released the token it held, so this phase adds no
        evidence of its own and can only wait.
        """
        self._next_action = now
        if self.counts().server_drain == 0:
            self._advance("restarting", now)

    def _try_start(self, now: float) -> None:
        """Clear the barrier, restart the cache, and give up after the limit.

        A start queued behind a barrier that is still there reaches a unit
        whose own condition skips it, so a barrier that cannot be removed is a
        failed step like any other: it is retried with backoff, and the
        operator is told to restore the runtime directory the barrier lives in.
        """
        # Released once the start has been accepted, not when it is decided. A
        # refused start leaves the server stopped while the launch decision
        # still holds the reading taken while it was healthy, and a compiler
        # composed in that state starts a server of its own inside its own
        # scope, where nothing can account for it. Keeping the denial is the
        # safe direction: it withholds composition, it can never release a
        # token, and the deadline the last tick set lapses on its own, so a
        # transaction that gives up cannot deny the cache for longer than the
        # retry bound.
        try:
            self.barrier.clear()
            self.cache.start()
        except (OSError, SystemdError) as error:
            # The phase and the counter are what make the last fault readable
            # as "restart, not accounting": the operator sees which step gave
            # up, and a completion that follows a refused start is its own
            # terminal count rather than a silent absence of one.
            self._fail(now, restart_fault(error, self.barrier))
            if self._attempts >= RECOVERY_FAILURE_LIMIT:
                self.metrics.emit(
                    "cache_recovery", phase=self._phase, recovery_failures=1
                )
                self._finish(now)
            return
        self.release_cache()
        # A fault from an earlier attempt of this same transaction is retired
        # here, so an idle transaction's last fault can only mean the final
        # transaction ended faulted — which is what the operator command reads.
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
        self._cooling = now + RECOVERY_COOLDOWN_SECONDS
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
        # One emission per failure rather than per distinct message: a retry
        # that repeats its reason is the case an operator most needs counted,
        # and the distinct-text rule still governs the journal line alone.
        self.metrics.emit("cache_recovery", phase=self._phase, recovery_attempts=1)
        if changed:
            with contextlib.suppress(OSError):
                log(f"cache recovery: {detail}")
        if self._attempts == RECOVERY_FAILURE_LIMIT:
            report_exhaustion(self._phase, self.metrics)
