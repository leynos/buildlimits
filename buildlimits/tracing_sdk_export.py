"""Adapt OpenTelemetry's exporter lifecycle to bounded journal records."""

import typing as typ

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from buildlimits.journal_native import JournalSink
from buildlimits.tracing_export import (
    TRACE_FAILURE_KINDS,
    TRACE_FAILURES,
    _failure_kind,
    _increment_trace_count,
    _record,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from opentelemetry.sdk.trace import ReadableSpan


class JournalSpanExporter(SpanExporter):
    """Export completed SDK spans as bounded native journal records."""

    def __init__(self, sink: JournalSink | None, source: str) -> None:
        """Bind a fixed producer and its optional journal transport."""
        self.sink = sink
        self.source = source
        self.dropped_spans = 0
        self.failure_counts = dict.fromkeys(TRACE_FAILURE_KINDS, 0)

    def export(self, spans: cabc.Sequence[ReadableSpan]) -> SpanExportResult:
        """Send completed spans, isolating record and transport failures.

        Returns
        -------
        SpanExportResult
            Whether every span in this synchronous batch was exported.
        """
        if self.sink is None:
            return SpanExportResult.SUCCESS
        failed = False
        for span in spans:
            try:
                record = _record(span, self.source)
                JournalSink.emit_trace(self.sink, record)
            except TRACE_FAILURES as error:
                failed = True
                category = _failure_kind(error)
                self.failure_counts[category] = _increment_trace_count(
                    self.failure_counts[category]
                )
                self.dropped_spans = _increment_trace_count(self.dropped_spans)
        return SpanExportResult.FAILURE if failed else SpanExportResult.SUCCESS
