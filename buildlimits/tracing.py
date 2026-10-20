"""Bounded OpenTelemetry spans over the native journal transport.

The adapter imports the OpenTelemetry SDK only after a restart needs a retry.
The supervisor starts a span for each scope placement. Exported attributes use
fixed vocabularies; the only generated identifiers are fixed-width trace IDs.
"""

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.journal_native import JournalSink
    from buildlimits.tracing_sdk_export import JournalSpanExporter

from buildlimits.tracing_export import (
    OPERATIONS,
    TRACE_FAILURE_KINDS,
    TRACE_FAILURES,
    _bounded_attributes,
    _failure_kind,
    _increment_trace_count,
)


class _OtelSpanPort(typ.Protocol):
    """The OpenTelemetry span methods used by the bounded adapter."""

    def set_attributes(self, attributes: cabc.Mapping[str, str | int]) -> None:
        """Set the fixed attributes accepted by the local journal schema."""

    def end(self) -> None:
        """Finish the span and pass it to its configured processor."""


class _OtelTracerPort(typ.Protocol):
    """The OpenTelemetry tracer method needed to start a placement span."""

    def start_span(
        self,
        name: str,
        *,
        attributes: cabc.Mapping[str, str | int],
    ) -> _OtelSpanPort:
        """Start a span with only the fixed initial attributes."""


class JournalTracer:
    """Create OpenTelemetry spans and export bounded records to journald.

    Parameters
    ----------
    source : str
        Fixed producer name, either ``supervisor`` or ``adapter``.
    sink : JournalSink | None
        Shared native journal transport; absent means spans are discarded.
    """

    def __init__(self, source: str, sink: JournalSink | None = None) -> None:
        """Bind a source and sink without importing or initializing the SDK.

        Raises
        ------
        ValueError
            If ``source`` is not one of the fixed producer names.
        """
        if source not in {"supervisor", "adapter"}:
            msg = "unknown tracing source"
            raise ValueError(msg)
        self.source = source
        self.sink = sink
        self._tracer: _OtelTracerPort | None = None
        self._exporter: JournalSpanExporter | None = None
        self._dropped_spans = 0
        self._failure_counts = dict.fromkeys(TRACE_FAILURE_KINDS, 0)

    @property
    def dropped_spans(self) -> int:
        """The capped number of spans lost before or during export."""
        exported = 0 if self._exporter is None else self._exporter.dropped_spans
        return min(1_000_000, self._dropped_spans + exported)

    @property
    def failure_counts(self) -> dict[str, int]:
        """Counts of bounded SDK, record-validation and transport failures.

        Returns
        -------
        dict[str, int]
            A copy of the failure counters, without exception text or types.
        """
        counts = dict(self._failure_counts)
        if self._exporter is not None:
            for category, count in self._exporter.failure_counts.items():
                counts[category] = min(1_000_000, counts[category] + count)
        return counts

    def start(self, operation: str) -> _OtelSpan | None:
        """Start one known operation, isolating SDK and provider failures.

        Returns
        -------
        _OtelSpan | None
            A real span, or ``None`` if the operation or SDK is unavailable.
        """
        if operation not in OPERATIONS:
            self._drop(ValueError("unknown trace operation"))
            return None
        try:
            tracer = self._get_tracer()
            span = tracer.start_span(
                operation,
                attributes={
                    "outcome": "unknown",
                    "failure_category": "none",
                    "stop_mode": "unknown",
                    "retry_attempt": 0,
                },
            )
        except TRACE_FAILURES as error:
            self._drop(error)
            return None
        return _OtelSpan(span, self)

    def _get_tracer(self) -> _OtelTracerPort:
        """Initialize a private provider on the first actual span.

        Returns
        -------
        _OtelTracerPort
            The OpenTelemetry tracer bound to this private provider.
        """
        if self._tracer is not None:
            return self._tracer
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON

        from buildlimits.tracing_sdk_export import JournalSpanExporter

        # Local journal spans must not disappear under an exporter-only
        # sampler setting; OTEL_SDK_DISABLED remains the explicit opt-out.
        provider = TracerProvider(sampler=ALWAYS_ON)
        self._exporter = JournalSpanExporter(self.sink, self.source)
        provider.add_span_processor(SimpleSpanProcessor(self._exporter))
        self._tracer = provider.get_tracer("buildlimits")
        return self._tracer

    def _drop(self, error: Exception) -> None:
        """Count a bounded failure category without retaining its details."""
        category = _failure_kind(error)
        self._failure_counts[category] = _increment_trace_count(
            self._failure_counts[category]
        )
        self._dropped_spans = _increment_trace_count(self._dropped_spans)


class _OtelSpan:
    """Failure-isolating adapter for one OpenTelemetry SDK span."""

    def __init__(self, span: _OtelSpanPort, owner: JournalTracer) -> None:
        """Retain the span and its value-free failure counter."""
        self._span = span
        self._owner = owner
        self._ended = False

    def set(self, **attributes: object) -> bool:
        """Apply bounded attributes once while the underlying span is active.

        Returns
        -------
        bool
            Whether attributes were applied while the span remained open.
        """
        if self._ended:
            return False
        values = _bounded_attributes(attributes)
        try:
            self._span.set_attributes(values)
        except TRACE_FAILURES as error:
            self._owner._drop(error)
        return True

    def finish(
        self,
        *,
        outcome: str,
        failure_category: str = "none",
        stop_mode: str = "none",
        retry_attempt: int = 0,
    ) -> None:
        """Record a terminal state and end exactly once, even if export fails."""
        if not self.set(
            outcome=outcome,
            failure_category=failure_category,
            stop_mode=stop_mode,
            retry_attempt=retry_attempt,
        ):
            return
        self._ended = True
        try:
            self._span.end()
        except TRACE_FAILURES as error:
            self._owner._drop(error)
