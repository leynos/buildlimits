"""Operator guidance for a recovery transaction that exhausted its attempts.

``CacheRecovery`` retries each failing step with backoff and stops retrying at
``RECOVERY_FAILURE_LIMIT``, setting ``last_fault`` permanently visible. The
text here is what the operator is told at that moment, one paragraph per phase
that can fail, so the remediation matches the step that actually broke.

A transaction that cannot write its barrier or stop the cache keeps holding
every token, so its guidance names what to restore rather than what to release.
A restart that cannot be issued is different: accounting has already completed,
so its guidance is about the cache alone.
"""

import contextlib
import typing as typ

from buildlimits.journal import log

if typ.TYPE_CHECKING:
    from buildlimits.metrics import MetricsPort

GUIDANCE: dict[str, str] = {
    "quiescing": (
        "The recovery barrier could not be written, so the managed sccache "
        "service may restart while its work is unaccounted. Restore the "
        "supervisor's runtime directory and re-run build-limits-recover; "
        "tokens remain held."
    ),
    "stopping": (
        "The managed sccache service could not be stopped. Check the systemd "
        "user manager and the unit's state, then re-run build-limits-recover; "
        "tokens remain held until the service is verified stopped and empty, "
        "and stopping sccache by hand does not release them."
    ),
    "restarting": (
        "The managed sccache service could not be restarted, or its recovery "
        "barrier could not be removed, after its work was accounted for. "
        "Restore the supervisor's runtime directory and re-run "
        "build-limits-recover; admission has already released the retained "
        "tokens and is compiling directly until the service returns."
    ),
}


def report_exhaustion(phase: str, metrics: MetricsPort) -> None:
    """Journal and count a failed phase that has stopped retrying.

    Parameters
    ----------
    phase : str
        The transaction phase whose attempts were exhausted.
    metrics : MetricsPort
        Port for the recovery exhaustion counter; a transaction is meant to
        exhaust at most once, so a second count for the same one would be a
        bug this surface makes visible.
    """
    with contextlib.suppress(OSError):
        log(f"cache recovery needs operator action: {GUIDANCE[phase]}")
    metrics.emit("cache_recovery", recovery_exhaustions=1, phase=phase)


def report_deadline_stop(waited: float, phase: str, metrics: MetricsPort) -> None:
    """Journal and count a stop taken on the roots deadline, not the proof.

    The policy that decides the stop lives in :mod:`buildlimits.recovery_roots`
    and knows nothing of the journal; this is the boundary that words it.

    Parameters
    ----------
    waited : float
        Seconds compiler roots stayed visible with no cached group moving.
    phase : str
        The transaction phase the count is reported under.
    metrics : MetricsPort
        Port for the bounded ``recovery_roots_deadline_stops`` counter.
    """
    with contextlib.suppress(OSError):
        log(
            "cache recovery: stopping the managed cache with compiler roots "
            f"still visible after {waited:.0f}s with no cached group moving"
        )
    metrics.emit("cache_recovery", phase=phase, recovery_roots_deadline_stops=1)
