"""Retry admission refusals while preserving the invocation's deadline."""

import contextlib
import dataclasses
import typing as typ

from buildlimits.adapter_connection import AdapterError
from buildlimits.adapter_invocation import (
    AdmissionAttempt,
    BusyError,
    RestartingError,
)
from buildlimits.heartbeat import write_line
from buildlimits.tracing import JournalTracer

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.heartbeat import Heartbeat
    from buildlimits.protocol import Exit
    from buildlimits.timing import RetryWait


RESTART_RETRY_SECONDS = 0.5


@dataclasses.dataclass(slots=True)
class _RetryState:
    """Keep one invocation's deadline and backoff state together."""

    attempt: AdmissionAttempt
    heartbeat: Heartbeat
    retry: RetryWait
    window: float = dataclasses.field(init=False)
    busy_delay: float = 0.05
    busy_announced: bool = False
    last_busy_refusal: BusyError | None = None

    def __post_init__(self) -> None:
        """Capture the initial unavailable window once for restart retries."""
        self.window = self.attempt.deadline - self.heartbeat.clock()

    def before_request(self) -> str | None:
        """Check deadline exhaustion and observe a retry after busy pressure.

        Returns
        -------
        str | None
            A bounded busy-deadline message when another request must not start.
        """
        previous = self.last_busy_refusal
        if previous is not None and self.heartbeat.clock() >= self.attempt.deadline:
            message = (
                "compile admission remains busy: "
                "descriptor pressure retry deadline expired; "
                f"last refusal: {ascii(str(previous)[:240])[1:-1]}"
            )
            self.attempt.summary.outcome = "refused"
            self.attempt.finish_restart(
                "retry_exhausted", failure_category="adapter_error"
            )
            return message
        self._observe_busy_retry(previous)
        return None

    def after_restart(self, error: RestartingError) -> bool:
        """Wait through a restart and account for the next physical attempt.

        Returns
        -------
        bool
            Whether the deadline permits another physical request.

        """
        self._observe_restart(error)
        with self._finish_restart_after_wait_failure():
            should_retry = self._wait_out_restart(error)
        if not should_retry:
            self.attempt.finish_restart("retry_exhausted")
            return False
        self.attempt.summary.retry_restart()
        self.last_busy_refusal = None
        return True

    def _observe_restart(self, error: RestartingError) -> None:
        """Open one retry span and refresh its bounded attempt attributes."""
        attempt = self.attempt
        attempt.restart_attempts = min(1_000_000, attempt.restart_attempts + 1)
        attempt.restart_failure_category = error.failure_category
        attempt.restart_stop_mode = error.stop_mode
        if attempt.restart_span is None:
            if attempt.tracer is None:
                attempt.tracer = JournalTracer("adapter", attempt.summary.trace_sink)
            attempt.restart_span = attempt.tracer.start("adapter_restart_retry")
        if attempt.restart_span is not None:
            attempt.restart_span.set(
                outcome="restarting",
                failure_category=attempt.restart_failure_category,
                stop_mode=attempt.restart_stop_mode,
                retry_attempt=attempt.restart_attempts,
            )

    def _wait_out_restart(self, error: RestartingError) -> bool:
        """Wait within the restart deadline, extending only queued requests.

        Returns
        -------
        bool
            Whether the deadline permits another physical request.
        """
        if not self.attempt.restart_announced:
            self.attempt.restart_announced = True
            write_line(
                "[build-limits] the supervisor is restarting; retrying admission"
            )
        now = self.heartbeat.clock()
        if error.recorded:
            self.attempt.deadline = max(self.attempt.deadline, now + self.window)
        remaining = self.attempt.deadline - now
        if remaining <= 0:
            self.attempt.summary.outcome = "refused"
            return False
        nap = RESTART_RETRY_SECONDS * self.retry.jitter(0.5, 1.0)
        self.retry.clock.sleep(min(remaining, nap))
        return True

    def after_busy(self, error: BusyError) -> None:
        """Wait once after descriptor pressure and increase capped backoff."""
        self.last_busy_refusal = error
        remaining = self.attempt.deadline - self.heartbeat.clock()
        if remaining <= 0:
            return
        if not self.busy_announced:
            self.busy_announced = True
            write_line(
                "[build-limits] waiting for descriptor headroom; retrying admission"
            )
        with self._finish_restart_after_wait_failure():
            self.retry.clock.sleep(
                min(remaining, self.busy_delay * self.retry.jitter(0.5, 1.0))
            )
        self.busy_delay = min(5.0, self.busy_delay * 2)

    @contextlib.contextmanager
    def _finish_restart_after_wait_failure(
        self,
    ) -> cabc.Generator[None]:
        """Finish a pending span when either retry wait is interrupted.

        Raises
        ------
        KeyboardInterrupt
            If the caller interrupts the retry wait.
        """
        try:
            yield
        except KeyboardInterrupt:
            self.attempt.finish_restart("cancelled", failure_category="cancelled")
            raise
        except Exception:
            self.attempt.finish_restart("failed", failure_category="adapter_error")
            raise

    def _observe_busy_retry(self, previous: BusyError | None) -> None:
        """Count only a physical attempt following a busy refusal."""
        if previous is not None:
            self.attempt.summary.retry_busy()


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
    KeyboardInterrupt
        If the caller interrupts admission or its retry wait.
    """
    state = _RetryState(attempt, heartbeat, retry)
    while True:
        busy_deadline_message = state.before_request()
        if busy_deadline_message is not None:
            raise AdapterError(busy_deadline_message) from state.last_busy_refusal
        try:
            result = request()
        except RestartingError as error:
            if not state.after_restart(error):
                raise
            continue
        except KeyboardInterrupt:
            attempt.finish_restart("cancelled", failure_category="cancelled")
            raise
        except BusyError as error:
            state.after_busy(error)
            continue
        except Exception:
            attempt.finish_restart("failed", failure_category="adapter_error")
            raise
        attempt.finish_restart("admitted")
        return result
