"""Bounded admission retries share one invocation deadline and metric summary."""

import dataclasses
import os
import typing as typ

from buildlimits.adapter_connection import AdapterError
from buildlimits.protocol import Refused
from buildlimits.tracing import JournalTracer, _OtelSpan

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.adapter_metrics import AdapterMetrics


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

    def __init__(
        self,
        message: str,
        *,
        failure_category: str = "supervisor_unavailable",
        stop_mode: str = "unknown",
    ) -> None:
        """Retain only fixed categories for the retry span."""
        super().__init__(message)
        self.failure_category = failure_category
        self.stop_mode = stop_mode


class QueuedRestartError(RestartingError):
    """A restart that interrupted a request which had been queued."""

    recorded = True


@dataclasses.dataclass(frozen=True, slots=True)
class Invocation:
    """What one adapter invocation asks the supervisor to run.

    Attributes
    ----------
    argv : Sequence[str]
        The real compiler's argument vector.
    cacheable : bool
        Whether the supervisor may compose the compiler with sccache.
    env : Mapping[str, str]
        The environment to send and to read the adapter settings from; a
        snapshot of this process's own unless the caller computed another.
    """

    argv: cabc.Sequence[str]
    cacheable: bool
    env: cabc.Mapping[str, str] = dataclasses.field(
        default_factory=lambda: dict(os.environ)
    )


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

    def restart(
        self,
        reason: str,
        *,
        failure_category: str = "supervisor_unavailable",
        stop_mode: str = "unknown",
    ) -> RestartingError:
        """Describe a supervisor lost before the compiler was admitted.

        Parameters
        ----------
        reason : str
            The refusal or connection fault.
        failure_category : str
            Fixed category for the trace summary.
        stop_mode : str
            Fixed stop mode when the supervisor reported one.

        Returns
        -------
        RestartingError
            The restart, marked queued when the request had been acknowledged.
        """
        return (QueuedRestartError if self.recorded else RestartingError)(
            reason,
            failure_category=failure_category,
            stop_mode=stop_mode,
        )


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
    tracer : JournalTracer | None
        Optional tracer, initialized lazily from the first restart otherwise.
    """

    deadline: float
    summary: AdapterMetrics
    restart_announced: bool = False
    tracer: JournalTracer | None = None
    restart_span: _OtelSpan | None = None
    restart_attempts: int = 0
    restart_failure_category: str = "unknown"
    restart_stop_mode: str = "unknown"

    def admitted(self) -> None:
        """End a restart span at admission, before the compiler starts."""
        self.finish_restart("admitted")

    def finish_restart(
        self,
        outcome: str,
        *,
        failure_category: str | None = None,
    ) -> None:
        """End a pending retry span once with bounded terminal attributes."""
        span, self.restart_span = self.restart_span, None
        if span is None:
            return
        _OtelSpan.finish(
            span,
            outcome=outcome,
            failure_category=failure_category or self.restart_failure_category,
            stop_mode=self.restart_stop_mode,
            retry_attempt=self.restart_attempts,
        )


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
            return progress.restart(
                message,
                failure_category="restart_refusal",
                stop_mode="graceful",
            )
        case Refused(reason="busy", message=message) if not progress.recorded:
            return BusyError(message)
        case _:
            return AdapterError(reply.message)
