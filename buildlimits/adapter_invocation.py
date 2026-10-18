"""Bounded admission retries share one invocation deadline and metric summary."""

import dataclasses
import typing as typ

from buildlimits.adapter_connection import AdapterError
from buildlimits.heartbeat import write_line
from buildlimits.protocol import Refused

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.adapter_metrics import AdapterMetrics
    from buildlimits.heartbeat import Heartbeat
    from buildlimits.protocol import Exit
    from buildlimits.timing import RetryWait


RESTART_RETRY_SECONDS = 0.5


class BusyError(AdapterError):
    """A request was explicitly refused before launch due to descriptor pressure."""


class RestartingError(AdapterError):
    """The supervisor went away before the compiler was admitted.

    Nothing ran for the request: the supervisor refused it as ``restarting``,
    or its connection ended before any admission reply. It is safe to submit the
    request again, which is what makes a supervisor restart invisible to builds.

    Attributes
    ----------
    recorded : bool
        Whether the supervisor had acknowledged the request as queued. Only then
        did the request wait, and only then is the wait not counted against the
        window in which a supervisor may be unavailable.
    """

    recorded = False


class QueuedRestartError(RestartingError):
    """A restart that interrupted a request which had been queued."""

    recorded = True


@dataclasses.dataclass(slots=True)
class Progress:
    """What the supervisor has acknowledged for one request so far.

    Attributes
    ----------
    recorded : bool
        The request was acknowledged as queued.
    admitted : bool
        The compiler was admitted, so it may have run.
    """

    recorded: bool = False
    admitted: bool = False

    def restart(self, reason: str) -> RestartingError:
        """Describe a supervisor lost before the compiler was admitted.

        Parameters
        ----------
        reason : str
            The refusal or connection fault.

        Returns
        -------
        RestartingError
            The restart, marked queued when the request had been acknowledged.
        """
        return (QueuedRestartError if self.recorded else RestartingError)(reason)


@dataclasses.dataclass(slots=True)
class AdmissionAttempt:
    """Admission attempts share the invocation's deadline and terminal observer.

    Attributes
    ----------
    deadline : float
        Overall monotonic deadline, never reset by a transient busy refusal.
        Only a restart moves it, to a fresh unavailability window.
    summary : AdapterMetrics
        One terminal observer shared by every physical admission attempt.
    """

    deadline: float
    summary: AdapterMetrics
    restart_announced: bool = False

    def wait_out_restart(
        self,
        error: RestartingError,
        heartbeat: Heartbeat,
        retry: RetryWait,
        window: float,
    ) -> None:
        """Pause before asking again, with a fresh window after a queued wait.

        The wait that preceded a restart was spent queued, not unavailable, so
        the new supervisor gets the whole window the invocation began with. A
        request that was never queued renews nothing, which keeps a supervisor
        that only ever refuses bounded by the original deadline.

        Parameters
        ----------
        error : RestartingError
            The restart the request ran into.
        heartbeat : Heartbeat
            Supplies the shared monotonic clock.
        retry : RetryWait
            Sleeper and jitter for the pause between attempts.
        window : float
            The unavailability window the invocation began with, in seconds.

        Raises
        ------
        RestartingError
            The same refusal, once the deadline has passed.
        """  # ruff: ignore[docstring-extraneous-exception] - the argument is re-raised.
        if not self.restart_announced:
            self.restart_announced = True
            write_line(
                "[build-limits] the supervisor is restarting; retrying admission"
            )
        now = heartbeat.clock()
        if error.recorded:
            self.deadline = max(self.deadline, now + window)
        remaining = self.deadline - now
        if remaining <= 0:
            self.summary.outcome = "refused"
            raise error
        nap = RESTART_RETRY_SECONDS * retry.jitter(0.5, 1.0)
        retry.clock.sleep(min(remaining, nap))


def refusal(reply: Refused, progress: Progress) -> AdapterError:
    """Classify a refusal as retryable or final.

    Parameters
    ----------
    reply : Refused
        The supervisor's refusal.
    progress : Progress
        What the supervisor had acknowledged before it refused.

    Returns
    -------
    AdapterError
        A restart or, before the request is recorded, a busy refusal, which the
        caller retries; otherwise the final failure with the supervisor's text.
    """
    match reply:
        case Refused(reason="restarting", message=message):
            return progress.restart(message)
        case Refused(reason="busy", message=message) if not progress.recorded:
            return BusyError(message)
        case _:
            return AdapterError(reply.message)


def retry_request(
    request: cabc.Callable[[], Exit],
    heartbeat: Heartbeat,
    attempt: AdmissionAttempt,
    retry: RetryWait,
) -> Exit:
    """Retry busy and restarting requests within their admission deadline.

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
    RestartingError
        If the supervisor keeps going away until the deadline has passed.
    """  # ruff: ignore[docstring-extraneous-exception] - the attempt re-raises it.
    delay = 0.05
    announced = False
    window = attempt.deadline - heartbeat.clock()
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
        except RestartingError as error:
            attempt.wait_out_restart(error, heartbeat, retry, window)
            last_refusal = None
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
