"""Bounded OpenTelemetry spans stay observational and safe to export."""

import os
import unittest.mock as mock  # ruff: ignore[manual-from-import] - module alias required by .rules/python-typing.md.
from pathlib import Path

import pytest
from journal_fixtures import journal_fields, journal_receiver

from buildlimits.journal_native import MAX_DATAGRAM_BYTES, JournalSink
from buildlimits.tracing import JournalTracer


def test_export_contains_only_bounded_attributes_and_fixed_width_trace_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known spans export despite sampler settings and stay bounded."""
    monkeypatch.setitem(os.environ, "OTEL_TRACES_SAMPLER", "always_off")
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


def test_export_dispatches_through_the_injected_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sink override receives the record produced by the exporter."""
    sink = JournalSink(tmp_path / "journal.sock")
    emit_trace = mock.Mock()
    monkeypatch.setattr(sink, "emit_trace", emit_trace)
    tracer = JournalTracer("supervisor", sink)
    span = tracer.start("scope_placement")
    assert span is not None, "the known operation starts a span"

    span.finish(
        outcome="admitted",
        failure_category="none",
        stop_mode="graceful",
    )

    assert emit_trace.call_count == 1, "export uses the injected instance method"
    record = emit_trace.call_args.args[0]
    assert record["BUILD_LIMITS_TRACE_OPERATION"] == "scope_placement", (
        "the override receives the fixed operation name"
    )
    assert record["BUILD_LIMITS_TRACE_OUTCOME"] == "admitted", (
        "the override receives the bounded span outcome"
    )


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


def test_sdk_initialization_failure_does_not_escape_span_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed SDK setup is counted and leaves tracing optional."""
    tracer = JournalTracer("adapter")
    monkeypatch.setattr(
        tracer,
        "_get_tracer",
        mock.Mock(side_effect=RuntimeError("private SDK detail")),
    )

    assert tracer.start("adapter_restart_retry") is None, (
        "SDK setup failure disables this span without reaching the caller"
    )
    assert tracer.dropped_spans == 1, "the failed span is counted"
    assert tracer.failure_counts == {
        "transport": 0,
        "invalid_record": 0,
        "sdk": 1,
    }, "only a bounded SDK failure count is retained"


@pytest.mark.parametrize(
    "failed_method",
    ["set_attributes", "end"],
    ids=["attribute-update", "span-end"],
)
def test_span_sdk_failure_does_not_escape_completion(
    monkeypatch: pytest.MonkeyPatch, failed_method: str
) -> None:
    """Attribute-update and end failures remain isolated from the caller."""
    tracer = JournalTracer("adapter")
    sdk_span = mock.Mock()
    getattr(sdk_span, failed_method).side_effect = RuntimeError("private SDK detail")
    sdk_tracer = mock.Mock()
    sdk_tracer.start_span.return_value = sdk_span
    monkeypatch.setattr(tracer, "_get_tracer", mock.Mock(return_value=sdk_tracer))
    span = tracer.start("adapter_restart_retry")
    assert span is not None, "the controlled SDK span starts"

    span.finish(outcome="admitted", failure_category="none")

    sdk_span.end.assert_called_once_with()
    assert tracer.dropped_spans == 1, "the SDK failure is counted"
    assert tracer.failure_counts == {
        "transport": 0,
        "invalid_record": 0,
        "sdk": 1,
    }, "the SDK failure text is not retained"
