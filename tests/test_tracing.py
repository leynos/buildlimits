"""Bounded OpenTelemetry spans stay observational and safe to export."""

from pathlib import Path

from journal_fixtures import journal_fields, journal_receiver

from buildlimits.journal_native import MAX_DATAGRAM_BYTES, JournalSink
from buildlimits.tracing import JournalTracer


def test_export_contains_only_bounded_attributes_and_fixed_width_trace_ids(
    tmp_path: Path,
) -> None:
    """Sensitive and unknown span data never crosses the journal boundary."""
    path = tmp_path / "journal.sock"
    sink = JournalSink(path)
    with journal_receiver(path) as receiver:
        try:
            tracer = JournalTracer("supervisor", sink)
            span = tracer.start("scope_placement")
            assert span is not None, "the known operation starts a span"
            span.set(
                outcome="admitted",
                failure_category="none",
                stop_mode="graceful",
                retry_attempt=1,
                request_path="/home/user/private.rs",
                raw_error="socket path leaked",
                unbounded_identifier="caller supplied",
            )
            span.finish(
                outcome="admitted",
                failure_category="none",
                stop_mode="graceful",
                retry_attempt=1,
            )
            record = journal_fields(receiver.recv(MAX_DATAGRAM_BYTES))
        finally:
            sink.close()
    assert record["BUILD_LIMITS_TRACE_OPERATION"] == "scope_placement", (
        "the record identifies the traced operation"
    )
    assert record["BUILD_LIMITS_TRACE_OUTCOME"] == "admitted", (
        "the record reports placement success"
    )
    assert record["BUILD_LIMITS_TRACE_FAILURE_CATEGORY"] == "none", (
        "successful placement has no failure category"
    )
    assert record["BUILD_LIMITS_TRACE_STOP_MODE"] == "graceful", (
        "the record carries the known stop mode"
    )
    assert record["BUILD_LIMITS_TRACE_RETRY_ATTEMPT"] == "1", (
        "the retry count is represented as a bounded integer field"
    )
    assert 0 <= int(record["BUILD_LIMITS_TRACE_DURATION_MS"]) <= 86_400_000, (
        "the duration stays within its one-day cap"
    )
    assert len(record["BUILD_LIMITS_TRACE_ID"]) == 32, (
        "the trace identifier has a fixed hexadecimal width"
    )
    assert len(record["BUILD_LIMITS_TRACE_SPAN_ID"]) == 16, (
        "the span identifier has a fixed hexadecimal width"
    )
    assert not any(
        "path" in key or "error" in key or "identifier" in key for key in record
    ), "unknown and sensitive attributes are omitted"


def test_journal_export_failure_does_not_escape_span_completion(
    tmp_path: Path,
) -> None:
    """A full or unavailable journal cannot change the observed operation."""
    path = tmp_path / "journal-file"
    path.touch()
    sink = JournalSink(path)
    tracer = JournalTracer("adapter", sink)
    span = tracer.start("adapter_restart_retry")
    assert span is not None, "the known operation starts a span"
    span.finish(
        outcome="admitted",
        failure_category="restart_refusal",
        stop_mode="graceful",
        retry_attempt=1,
    )

    assert tracer.dropped_spans == 1, "the failed export is counted without escaping"
    assert tracer.failure_counts == {
        "transport": 1,
        "invalid_record": 0,
        "sdk": 0,
    }, "the failure is classified without retaining the exception"
    sink.close()
