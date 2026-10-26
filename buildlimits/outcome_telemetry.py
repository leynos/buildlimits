"""Publish recorder effects through the supervisor's bounded observations."""

import typing as typ

from buildlimits.outcome_recorder_types import QueryFinished, QueryStarted

if typ.TYPE_CHECKING:
    from buildlimits.metrics import MetricsPort
    from buildlimits.outcome_recorder_types import Outputs
    from buildlimits.tracing import JournalTracer


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
        """Bind the observation ports without starting a span."""
        self._metrics = metrics
        self._tracer = tracer
        self._query_span: _QuerySpanPort | None = None

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
            is_failure = reason != "none"
            self._metrics.emit(
                "outcome_reading",
                outcome_reason=reason,
                readings=1,
                readings_failed=int(is_failure),
            )
        for transition in outputs.spans:
            match transition:
                case QueryStarted():
                    self._start_query(transition)
                case QueryFinished():
                    self._finish_query(transition)

    def reading_skipped(self, reason: str) -> None:
        """Count one scheduler decision which deliberately did not read."""
        self._metrics.emit("outcome_reading", gate_reason=reason, readings_skipped=1)

    def child_sample(self, *, failed: bool) -> None:
        """Aggregate one full-emit sampling success or failure."""
        field = "child_sample_failures" if failed else "child_samples_taken"
        self._metrics.emit("outcome_reading", **{field: 1})

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
