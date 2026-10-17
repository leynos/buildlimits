"""Bounded admission retries share one invocation deadline and metric summary."""

import dataclasses
import typing as typ

from buildlimits.adapter_connection import AdapterError
from buildlimits.heartbeat import write_line

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.adapter_metrics import AdapterMetrics
    from buildlimits.heartbeat import Heartbeat
    from buildlimits.protocol import Exit
    from buildlimits.timing import RetryWait


class BusyError(AdapterError):
    """A request was explicitly refused before launch due to descriptor pressure."""


@dataclasses.dataclass(frozen=True, slots=True)
class AdmissionAttempt:
    """Admission attempts share the invocation's deadline and terminal observer.

    Attributes
    ----------
    deadline : float
        Overall monotonic deadline, never reset by a transient busy refusal.
    summary : AdapterMetrics
        One terminal observer shared by every physical admission attempt.
    """

    deadline: float
    summary: AdapterMetrics


def retry_request(
    request: cabc.Callable[[], Exit],
    heartbeat: Heartbeat,
    attempt: AdmissionAttempt,
    retry: RetryWait,
) -> Exit:
    """Retry only unrecorded busy requests within their original deadline.

    Parameters
    ----------
    request : Callable[[], Exit]
        Recaptures descriptors and performs one admission exchange.
    heartbeat : Heartbeat
        Supplies the shared monotonic clock and caller-facing progress.
    attempt : AdmissionAttempt
        Invocation deadline and its terminal observer.
    retry : RetryWait
        Sleeper and bounded jitter policy for exponential backoff.

    Returns
    -------
    Exit
        The recorded compiler's terminal result.

    Raises
    ------
    AdapterError
        If repeated busy refusals exhaust the original admission deadline.
    """
    delay = 0.05
    announced = False
    last_refusal: BusyError | None = None
    while True:
        if last_refusal is not None and heartbeat.clock() >= attempt.deadline:
            msg = (
                "compile admission remains busy: "
                "descriptor pressure retry deadline expired; "
                f"last refusal: {ascii(str(last_refusal)[:240])[1:-1]}"
            )
            attempt.summary.outcome = "refused"
            raise AdapterError(msg) from last_refusal
        _observe_busy_retry(attempt, last_refusal)
        try:
            return request()
        except BusyError as error:
            last_refusal = error
            remaining = attempt.deadline - heartbeat.clock()
            if remaining <= 0:
                continue
            if not announced:
                announced = True
                write_line(
                    "[build-limits] waiting for descriptor headroom; retrying admission"
                )
            retry.clock.sleep(min(remaining, delay * retry.jitter(0.5, 1.0)))
            delay = min(5.0, delay * 2)


def _observe_busy_retry(attempt: AdmissionAttempt, previous: BusyError | None) -> None:
    """Observe a fresh retry only when an earlier request was refused as busy."""
    if previous is not None:
        attempt.summary.retry_busy()
