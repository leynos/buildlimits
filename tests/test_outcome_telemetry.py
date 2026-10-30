"""Reading telemetry is transition-only and its counters never double count."""

import typing as typ

from outcome_shell_test_support import RecordingMetrics

from buildlimits.outcome_recorder_types import Outputs
from buildlimits.outcome_telemetry import READING_SUMMARY_SECONDS, OutcomeTelemetry

if typ.TYPE_CHECKING:
    from buildlimits.outcome_vocabulary import OutcomeReason


def _reading(*reasons: OutcomeReason) -> Outputs:
    """Build recorder outputs that carry only completed reading reasons."""
    return Outputs(reading_reasons=reasons)


def _reading_events(metrics: RecordingMetrics) -> list[dict[str, object]]:
    """Return the outcome_reading field mappings in emit order."""
    return [fields for event, fields in metrics.events if event == "outcome_reading"]


def _total(metrics: RecordingMetrics, name: str) -> int:
    """Sum one counter across every emitted outcome_reading event.

    Returns
    -------
    int
        The counter total, treating an omitted value as zero.
    """
    return sum(
        typ.cast("int", fields.get(name, 0)) for fields in _reading_events(metrics)
    )


def test_repeated_failures_cost_one_transition_and_one_summary() -> None:
    """A failing loop held for one minute writes two events, not sixty."""
    metrics = RecordingMetrics()
    telemetry = OutcomeTelemetry(metrics)

    for second in range(60):
        telemetry.publish(_reading("timeout"))
        telemetry.flush(float(second))
    telemetry.flush(READING_SUMMARY_SECONDS)

    assert _reading_events(metrics) == [
        {"outcome_reason": "timeout"},
        {"readings": 60, "readings_failed": 60},
    ], "an unchanged reason is reported once, then folded into the summary"
    assert _total(metrics, "readings") == 60, "the summary counts every reading"
    assert _total(metrics, "readings_failed") == 60, "every reading failed"


def test_reason_changes_are_transitions_and_totals_stay_exact() -> None:
    """Transitions carry labels only, so totals are never counted twice."""
    metrics = RecordingMetrics()
    telemetry = OutcomeTelemetry(metrics)

    for reason in ("timeout", "timeout", "none", "none", "timeout"):
        telemetry.publish(_reading(reason))
    telemetry.reading_skipped("queued")
    telemetry.reading_skipped("queued")
    telemetry.child_sample(failed=False)
    telemetry.child_sample(failed=True)
    telemetry.flush(0.0)
    telemetry.flush(READING_SUMMARY_SECONDS)

    assert _reading_events(metrics) == [
        {"outcome_reason": "timeout"},
        {"outcome_reason": "none"},
        {"outcome_reason": "timeout"},
        {"gate_reason": "queued"},
        {
            "readings": 5,
            "readings_skipped": 2,
            "readings_failed": 3,
            "child_samples_taken": 1,
            "child_sample_failures": 1,
        },
    ], "each changed state is reported once, then the interval is summarized"
    assert _total(metrics, "readings") == 5, "five readings happened"
    assert _total(metrics, "readings_failed") == 3, "three of them failed"
    assert _total(metrics, "readings_skipped") == 2, "two gates skipped"
    assert _total(metrics, "child_samples_taken") == 1, "one sample succeeded"
    assert _total(metrics, "child_sample_failures") == 1, "one sample failed"


def test_forced_flush_emits_the_partial_interval_once() -> None:
    """Shutdown reports the remaining counts, and a second call is silent."""
    metrics = RecordingMetrics()
    telemetry = OutcomeTelemetry(metrics)
    telemetry.child_sample(failed=False)

    telemetry.flush(0.5, force=True)
    telemetry.flush(0.6, force=True)

    assert _reading_events(metrics) == [{"child_samples_taken": 1}], (
        "the forced summary is bounded to the pending interval"
    )


def test_idle_minutes_emit_no_summary() -> None:
    """A recorder with nothing pending does not write an empty event."""
    metrics = RecordingMetrics()
    telemetry = OutcomeTelemetry(metrics)

    for minute in range(3):
        telemetry.flush(float(minute) * READING_SUMMARY_SECONDS)

    assert _reading_events(metrics) == [], "no counters means no journal noise"
