"""Publish recorder effects through the supervisor's bounded observations."""

import typing as typ

from buildlimits.outcome_recorder_types import QueryFinished, QueryStarted

if typ.TYPE_CHECKING:
    from buildlimits.metrics import MetricsPort
    from buildlimits.outcome_recorder_types import Outputs
    from buildlimits.tracing import JournalTracer

READING_SUMMARY_SECONDS = 60.0
"""Longest interval between aggregate ``outcome_reading`` summary events."""

_READING_COUNTERS = (
    "readings",
    "readings_skipped",
    "readings_failed",
    "child_samples_taken",
    "child_sample_failures",
)


class _QuerySpanPort(typ.Protocol):
    """The span operation needed to finish one statistics query."""

    def finish(
        self,
        *,
        outcome: str,
        failure_category: str = "none",
        stop_mode: str = "none",
        retry_attempt: int = 0,
    ) -> None:
        """Publish the query's fixed terminal classification."""


class OutcomeTelemetry:
    """Map recorder values to the fixed metrics and tracing catalogues.

    ``outcome_reading`` is deliberately not one event per decision. A reading
    failure retried every second would otherwise write roughly 86,400 events a
    day, which is the journal flooding D6 and R3 set out to prevent. Instead the
    event is emitted when the reading state changes, plus a bounded summary of
    the interval's counters, so a steady state costs one event a minute.

    Parameters
    ----------
    metrics : MetricsPort
        Failure-isolated supervisor event transport.
    tracer : JournalTracer | None
        Optional span producer.
    """

    def __init__(
        self, metrics: MetricsPort, tracer: JournalTracer | None = None
    ) -> None:
        """Bind the observation ports and start an empty reading interval."""
        self._metrics = metrics
        self._tracer = tracer
        self._query_span: _QuerySpanPort | None = None
        self._pending: dict[str, int] = dict.fromkeys(_READING_COUNTERS, 0)
        self._last_label: tuple[str, str] | None = None
        self._next_summary_at: float | None = None

    def publish(self, outputs: Outputs) -> None:
        """Publish completed outcome events, reasons and query transitions."""
        for event in outputs.events:
            self._metrics.emit(
                "cache_outcome",
                cache_result=event.cache_result,
                outcome_attribution=event.outcome_attribution,
                crate_origin=event.crate_origin,
                outcome_reason=event.outcome_reason,
                runtime_seconds=event.runtime_seconds,
                window_groups=event.window_groups,
                child_sighting=event.child_sighting,
                child_samples=event.child_samples,
            )
        for reason in outputs.reading_reasons:
            self._pending["readings"] += 1
            self._pending["readings_failed"] += int(reason != "none")
            self._transition("outcome_reason", reason)
        for transition in outputs.spans:
            match transition:
                case QueryStarted():
                    self._start_query(transition)
                case QueryFinished():
                    self._finish_query(transition)

    def reading_skipped(self, reason: str) -> None:
        """Count one gate skip and report a changed reading state.

        Examples
        --------
        A gate that stays closed reports its reason once, not once a tick.
        """
        self._pending["readings_skipped"] += 1
        self._transition("gate_reason", reason)

    def child_sample(self, *, failed: bool) -> None:
        """Accumulate one full-emit sampling success or failure."""
        field = "child_sample_failures" if failed else "child_samples_taken"
        self._pending[field] += 1

    def flush(self, now: float, *, force: bool = False) -> None:
        """Emit the interval's reading counters at most once a minute.

        Parameters
        ----------
        now : float
            The supervisor's monotonic clock.
        force : bool
            Emit whatever is pending immediately, as shutdown requires.

        Examples
        --------
        Forced at stop, the summary still carries the final partial interval.
        """
        if self._next_summary_at is None:
            self._next_summary_at = now + READING_SUMMARY_SECONDS
        counts = {name: value for name, value in self._pending.items() if value}
        if not counts:
            return
        if not force and now < self._next_summary_at:
            return
        self._metrics.emit("outcome_reading", **counts)
        self._pending = dict.fromkeys(_READING_COUNTERS, 0)
        self._next_summary_at = now + READING_SUMMARY_SECONDS

    def store_result(
        self, *, operation: str, reason: str, records: int, discarded: int
    ) -> None:
        """Publish one aggregated asynchronous writer result."""
        self._metrics.emit(
            "outcome_store",
            store_operation=operation,
            store_reason=reason,
            store_records=records,
            store_discarded=discarded,
        )

    def selection(self, *, is_quiet: bool) -> None:
        """Trace one due quiet-point decision without adding identifiers."""
        if self._tracer is None:
            return
        span = self._tracer.start("admission_selection")
        if span is not None:
            span.finish(outcome="completed" if is_quiet else "cancelled")

    def _transition(self, field: str, reason: str) -> None:
        """Emit one reading state change, suppressing an unchanged one."""
        label = (field, reason)
        if label == self._last_label:
            return
        self._last_label = label
        self._metrics.emit("outcome_reading", **{field: reason})

    def _start_query(self, _transition: QueryStarted) -> None:
        """Open the one span that represents a live statistics query."""
        if self._tracer is not None:
            self._query_span = self._tracer.start("statistics_read")

    def _finish_query(self, transition: QueryFinished) -> None:
        """Close a query span after its reducer has chosen a bounded reason."""
        span, self._query_span = self._query_span, None
        if span is None:
            return
        reason = transition.reason
        span.finish(
            outcome="completed" if reason == "none" else "failed",
            failure_category="none" if reason == "none" else "statistics_query",
        )
