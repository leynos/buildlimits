"""H3 replay requires balanced summaries and a non-vacuous sample."""

import dataclasses as dc

from outcome_test_helpers import OBSERVED_AT, cache_key

from buildlimits.outcome_record_format import OutcomeRecord, WindowSummary
from buildlimits.outcome_replay import evaluate_h3


def _outcomes(count: int) -> tuple[OutcomeRecord, ...]:
    """Build a fixed attributed sample of cacheable launches."""
    record = OutcomeRecord(
        key=cache_key(),
        outcome="hit",
        origin="registry",
        runtime_seconds=0.25,
        observed_at=OBSERVED_AT,
        parser_class="cacheable",
        child_sighting="unavailable",
        child_samples=0,
    )
    return (record,) * count


def _summary(**changes: int) -> WindowSummary:
    """Build a balanced refusal-summary row with a replaceable prediction."""
    fields = {
        "observed_at": OBSERVED_AT,
        "server_version": "0.14.0",
        "window_groups": 2_000,
        "requests_not_compile": 0,
        "requests_not_cacheable": 0,
        "predicted_not_compile": 0,
        "predicted_declined": 0,
        "predicted_cacheable": 2_000,
    }
    fields.update(changes)
    return WindowSummary(**fields)


def test_h3_support_requires_two_thousand_attributed_rows() -> None:
    """A balanced summary supports H3 only at its fixed sample minimum."""
    report = evaluate_h3((*_outcomes(2_000), _summary()))

    assert report.verdict == "supported", "the required sample supports the model"
    assert report.attributed_outcomes == 2_000, "the sample counts attributed rows"
    assert report.evaluated_windows == 1, "the summary is independently evaluated"


def test_one_refusal_counterexample_refutes_h3() -> None:
    """The two parser refusal classes are compared independently."""
    report = evaluate_h3((*_outcomes(2_000), _summary(predicted_not_compile=1)))

    assert report.verdict == "refuted", "one mismatched refusal count refutes H3"
    assert report.counterexamples == 1, "the mismatch is counted once"


def test_h3_is_insufficient_below_the_attributed_minimum() -> None:
    """Example rows cannot substitute for the prescribed evidence volume."""
    report = evaluate_h3((*_outcomes(1_999), _summary(window_groups=1_999)))

    assert report.verdict == "insufficient", "the evidence is below threshold"


def test_a_solo_refusal_row_is_a_secondary_counterexample() -> None:
    """An attributed refusal row can falsify its parser class directly."""
    mismatch = dc.replace(_outcomes(1)[0], outcome="hit", parser_class="not_compile")

    report = evaluate_h3((mismatch,))

    assert report.verdict == "refuted", "a direct parser mismatch is decisive"
    assert report.evaluated_rows == 1, "the refusal row is a secondary check"
