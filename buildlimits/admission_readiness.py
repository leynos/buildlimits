"""Purely classify whether the admission ledger can make progress.

Supervisor health describes the process and its token pool. Admission
readiness answers a separate question: whether a request can run now, is
waiting for normal capacity, or is blocked on operator recovery. Cache
reachability is deliberately absent because direct compilation can still
admit work safely.
"""

import dataclasses as dc
import typing as typ

# CrossHair resolves this dataclass field annotation at runtime.
from buildlimits.admission_state import Counts  # ruff: ignore[typing-only-first-party-import]

type AdmissionState = typ.Literal[
    "blocked", "closing", "draining", "ready", "saturated", "unhealthy"
]

_READY_STATES = frozenset({"ready", "saturated", "draining"})


def _counted(count: int, noun: str) -> str:
    """Render a count with the singular or plural form of a noun.

    Returns
    -------
    str
        The number followed by its grammatically matched noun.

    Examples
    --------
    >>> _counted(1, "slot"), _counted(2, "slot")
    ('1 slot', '2 slots')
    """
    plural = noun if count == 1 else f"{noun}s"
    return f"{count} {plural}"


@dc.dataclass(frozen=True, slots=True, kw_only=True)
class ReadinessFacts:
    """Immutable ledger and recovery facts for one readiness decision.

    Attributes
    ----------
    healthy : bool
        Whether the supervisor and its token pool are healthy.
    closing : bool
        Whether the supervisor is winding down for a graceful stop.
    counts : Counts
        The authoritative token, compiler-group, and queue snapshot. Cache
        composition is not used to classify readiness.
    drain_fault_kind : str | None
        The most recent failed accounting probe's kind, if one exists.
    recovery_automatic : bool
        Whether the configured policy can open recovery automatically.
    recovery_phase : str
        The current recovery phase, or ``idle`` when no transaction runs.
    recovery_attempts : int
        Failed attempts in the current recovery transaction.
    recovery_pending : bool
        Whether recovery is due or running with attempts remaining.

    Examples
    --------
    >>> facts = ReadinessFacts(
    ...     healthy=True, closing=False,
    ...     counts=Counts(
    ...         capacity=2, held=0, running=0, draining=0, server_drain=0,
    ...         queued=0, cached_moving=0,
    ...     ),
    ...     drain_fault_kind=None,
    ...     recovery_automatic=False, recovery_phase="idle",
    ...     recovery_attempts=0, recovery_pending=False,
    ... )
    >>> classify(facts).state
    'ready'
    """

    healthy: bool
    closing: bool
    counts: Counts
    drain_fault_kind: str | None
    recovery_automatic: bool
    recovery_phase: str
    recovery_attempts: int
    recovery_pending: bool


@dc.dataclass(frozen=True, slots=True)
class AdmissionReadiness:
    """The stable admission state and its short count-only explanation.

    Attributes
    ----------
    state : AdmissionState
        The readiness classification.
    reason : str
        A count-only explanation with no request or group identifiers.
    """

    state: AdmissionState
    reason: str

    @property
    def ready(self) -> bool:
        """Whether the classified state permits normal admission checks."""
        return self.state in _READY_STATES


def classify(facts: ReadinessFacts) -> AdmissionReadiness:
    """Classify progress from one read-only admission snapshot.

    Precedence is unhealthy, closing, operator-blocked full server drain,
    progressing full server drain, ordinary saturation, then available
    capacity. An automatic idle trigger with no queued demand is not treated as
    blocked while attempts remain: its first waiter opens the starvation
    trigger. A stopped recovery outlook remains blocked, even with no demand.

    Parameters
    ----------
    facts : ReadinessFacts
        The ledger, immediate drain fault and recovery outlook at one instant.

    Returns
    -------
    AdmissionReadiness
        The state and stable explanation for status consumers.

    Examples
    --------
    >>> classify(ReadinessFacts(
    ...     healthy=True, closing=False,
    ...     counts=Counts(
    ...         capacity=2, held=2, running=2, draining=0, server_drain=0,
    ...         queued=1, cached_moving=0,
    ...     ),
    ...     drain_fault_kind=None,
    ...     recovery_automatic=False, recovery_phase="idle",
    ...     recovery_attempts=0, recovery_pending=False,
    ... )).ready
    True
    """
    if not facts.healthy:
        return AdmissionReadiness("unhealthy", "supervisor is unhealthy")
    if facts.closing:
        return AdmissionReadiness("closing", "supervisor is closing")

    if facts.counts.capacity > 0 and facts.counts.server_drain >= facts.counts.capacity:
        return _classify_full_server_drain(facts)
    return _classify_other_capacity(facts)


def _classify_full_server_drain(facts: ReadinessFacts) -> AdmissionReadiness:
    """Tell a progressing retained drain from one that needs operator action.

    Returns
    -------
    AdmissionReadiness
        The retained-drain progress or operator-blocked state.

    Examples
    --------
    >>> _classify_full_server_drain(ReadinessFacts(
    ...     healthy=True, closing=False,
    ...     counts=Counts(
    ...         capacity=1, held=1, running=0, draining=0, server_drain=1,
    ...         queued=1, cached_moving=0,
    ...     ),
    ...     drain_fault_kind="live_work", recovery_automatic=False,
    ...     recovery_phase="idle", recovery_attempts=0,
    ...     recovery_pending=False,
    ... )).state
    'blocked'
    """
    recovery_attempts_stopped = _recovery_attempts_stopped(facts)
    automatic_waits_for_demand = _automatic_recovery_waits_for_demand(
        facts, recovery_attempts_stopped=recovery_attempts_stopped
    )
    operator_recovery_required = _operator_recovery_required(
        facts, automatic_waits_for_demand=automatic_waits_for_demand
    )
    capacity = facts.counts.capacity
    retained = f"{_counted(capacity, 'server-drain slot')} retained"
    if operator_recovery_required:
        if recovery_attempts_stopped:
            attempts = _counted(facts.recovery_attempts, "attempt")
            reason = f"{retained}; recovery stopped after {attempts}"
        else:
            reason = f"{retained}; operator recovery required"
        return AdmissionReadiness("blocked", reason)

    progress = _drain_progress(
        facts, automatic_waits_for_demand=automatic_waits_for_demand
    )
    return AdmissionReadiness("draining", f"{retained}; {progress}")


def _automatic_recovery_waits_for_demand(
    facts: ReadinessFacts, *, recovery_attempts_stopped: bool
) -> bool:
    """Check whether automatic recovery waits for demand at zero queue.

    Parameters
    ----------
    facts : ReadinessFacts
        The automatic policy and demand at the classification instant.
    recovery_attempts_stopped : bool
        Whether an idle recovery outlook has exhausted its attempts.

    Returns
    -------
    bool
        Whether recovery waits for its first queued request.

    Examples
    --------
    >>> from types import SimpleNamespace as Namespace
    >>> facts = Namespace(
    ...     recovery_automatic=True, recovery_phase="idle",
    ...     counts=Namespace(queued=0),
    ... )
    >>> _automatic_recovery_waits_for_demand(
    ...     facts, recovery_attempts_stopped=False
    ... )
    True
    """
    return (
        facts.recovery_automatic
        and _recovery_is_idle(facts.recovery_phase)
        and facts.counts.queued == 0
        and not recovery_attempts_stopped
    )


def _operator_recovery_required(
    facts: ReadinessFacts, *, automatic_waits_for_demand: bool
) -> bool:
    """Return whether the retained drain needs an operator action.

    Parameters
    ----------
    facts : ReadinessFacts
        The current fault kind and recovery outlook.
    automatic_waits_for_demand : bool
        Whether the first queued request opens automatic recovery.

    Returns
    -------
    bool
        Whether a current retained-drain fault has no safe progress path.
    """
    if facts.drain_fault_kind is None:
        return False
    if facts.drain_fault_kind != "live_work":
        return True
    return (
        _recovery_is_idle(facts.recovery_phase)
        and not facts.recovery_pending
        and not automatic_waits_for_demand
    )


def _recovery_is_idle(recovery_phase: str) -> bool:
    """Return whether recovery has no active phase.

    Returns
    -------
    bool
        Whether the phase is idle.

    Examples
    --------
    >>> _recovery_is_idle("idle")
    True
    """
    return recovery_phase == "idle"


def _recovery_attempts_stopped(facts: ReadinessFacts) -> bool:
    """Tell whether automatic recovery has finished all recorded attempts.

    A non-idle phase still has a progress path, even after its attempt budget
    reaches zero; only an idle, non-pending outlook has stopped.

    Parameters
    ----------
    facts : ReadinessFacts
        The recovery phase and retry outlook at the classification instant.

    Returns
    -------
    bool
        Whether the recovery outlook is idle after at least one attempt.

    Examples
    --------
    >>> _recovery_attempts_stopped(ReadinessFacts(
    ...     healthy=True, closing=False,
    ...     counts=Counts(
    ...         capacity=1, held=1, running=0, draining=0, server_drain=1,
    ...         queued=1, cached_moving=0,
    ...     ),
    ...     drain_fault_kind="live_work", recovery_automatic=True,
    ...     recovery_phase="idle", recovery_attempts=1,
    ...     recovery_pending=False,
    ... ))
    True
    """
    return (
        _recovery_is_idle(facts.recovery_phase)
        and facts.recovery_attempts > 0
        and not facts.recovery_pending
    )


def _drain_progress(facts: ReadinessFacts, *, automatic_waits_for_demand: bool) -> str:
    """Name the current progress path for a full server drain.

    Parameters
    ----------
    facts : ReadinessFacts
        The recovery phase and outlook at the classification instant.
    automatic_waits_for_demand : bool
        Whether the idle trigger waits for its first queued request.

    Returns
    -------
    str
        A stable explanation for the progress path.

    Examples
    --------
    >>> _drain_progress(
    ...     ReadinessFacts(
    ...         healthy=True, closing=False,
    ...         counts=Counts(
    ...             capacity=1, held=1, running=0, draining=0,
    ...             server_drain=1, queued=1, cached_moving=0,
    ...         ),
    ...         drain_fault_kind="live_work", recovery_automatic=True,
    ...         recovery_phase="idle", recovery_attempts=0,
    ...         recovery_pending=True,
    ...     ),
    ...     automatic_waits_for_demand=False,
    ... )
    'automatic recovery is pending'
    """
    if facts.recovery_phase != "idle":
        return f"recovery is in {facts.recovery_phase}"
    if facts.recovery_pending:
        return "automatic recovery is pending"
    return (
        "automatic recovery waits for demand"
        if automatic_waits_for_demand
        else "drain accounting is active"
    )


def _classify_other_capacity(facts: ReadinessFacts) -> AdmissionReadiness:
    """Report normal saturation or the capacity left beside a partial drain.

    Returns
    -------
    AdmissionReadiness
        The normal capacity state and its count-only reason.

    Examples
    --------
    >>> _classify_other_capacity(ReadinessFacts(
    ...     healthy=True, closing=False,
    ...     counts=Counts(
    ...         capacity=2, held=2, running=2, draining=0, server_drain=0,
    ...         queued=1, cached_moving=0,
    ...     ),
    ...     drain_fault_kind=None,
    ...     recovery_automatic=False, recovery_phase="idle",
    ...     recovery_attempts=0, recovery_pending=False,
    ... )).state
    'saturated'
    """
    counts = facts.counts
    if counts.capacity > 0 and counts.held >= counts.capacity:
        reason = f"all {_counted(counts.capacity, 'slot')} are occupied"
        if counts.server_drain > 0:
            retained = _counted(counts.server_drain, "server-drain slot")
            reason += f"; {retained} retained"
        return AdmissionReadiness("saturated", reason)

    available = max(0, counts.capacity - counts.held)
    if counts.server_drain > 0:
        retained = _counted(counts.server_drain, "server-drain slot")
        reason = (
            f"{retained} retained; {available} of {counts.capacity} slots available"
        )
    else:
        reason = f"{available} of {_counted(counts.capacity, 'slot')} available"
    return AdmissionReadiness("ready", reason)
