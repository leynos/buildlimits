"""Known-answer contracts for the per-row scores and the calibration bins."""

import datetime as dt
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from buildlimits.predict.metrics import (
    ScoredRow,
    _accuracy,
    brier_of,
    calibrate,
    log_loss_of,
    summarize,
)

STAMP = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)


def _row(probability: float | None, outcome: int) -> ScoredRow:
    """Return one scored row with a placeholder identity and origin."""
    return ScoredRow(
        predictor="steady",
        identity="0" * 64,
        origin="registry",
        observed_at=STAMP,
        probability=probability,
        outcome=outcome,
        phase="held-out",
    )


def test_brier_is_the_squared_error_of_one_answer() -> None:
    """The Brier score charges a confident wrong answer more than a mild one."""
    scores = {outcome: brier_of(0.7, outcome) for outcome in (1, 0)}

    assert scores == pytest.approx({1: 0.09, 0: 0.49}), "one hit, one miss"


def test_a_certain_answer_stays_finite_under_the_clip() -> None:
    """A probability of 0 or 1 is clipped, so its loss is large but finite."""
    losses = {
        "certain hit": log_loss_of(1.0, 1, clip=1e-6),
        "certain miss": log_loss_of(0.0, 0, clip=1e-6),
        "confident error": log_loss_of(0.0, 1, clip=1e-6),
    }

    assert losses == pytest.approx({
        "certain hit": 1e-6,
        "certain miss": 1e-6,
        "confident error": -math.log(1e-6),
    }), "the clip keeps every answer finite"


@pytest.mark.parametrize(
    ("probability", "outcome", "expected"),
    [
        pytest.param(0.5, 1, 1.0, id="at-the-threshold-is-a-hit"),
        pytest.param(0.499, 1, 0.0, id="below-the-threshold-is-a-miss"),
        pytest.param(0.0, 0, 1.0, id="a-confident-miss-is-right"),
        pytest.param(1.0, 0, 0.0, id="a-confident-hit-is-wrong"),
    ],
)
def test_accuracy_counts_the_thresholded_decision(
    probability: float, outcome: int, expected: float
) -> None:
    """A probability at or above the threshold is a hit, and below it a miss."""
    assert _accuracy([(probability, outcome)], 0.5) == pytest.approx(expected), (
        "the decision follows the threshold"
    )


def test_calibration_places_boundaries_in_the_upper_bin() -> None:
    """A row on a bin's lower edge belongs to that bin, and the last bin holds 1.0."""
    bins = calibrate([_row(0.2, 1), _row(1.0, 0)], bins=5)

    assert [bin_.count for bin_ in bins] == [0, 1, 0, 0, 1], "edges are inclusive"
    assert bins[1].lower == pytest.approx(0.2), "the edge names the bin"
    assert bins[4].upper == pytest.approx(1.0), "the last bin closes the range"


def test_calibration_reports_the_mean_answer_and_the_observed_rate() -> None:
    """Each bin carries what was predicted and what actually happened."""
    bins = calibrate([_row(0.6, 1), _row(0.7, 0)], bins=5)

    assert bins[3].count == 2, "both rows share the bin"
    assert bins[3].mean_probability == pytest.approx(0.65), "the mean answer"
    assert bins[3].observed_rate == pytest.approx(0.5), "one hit of two"


def test_calibration_excludes_abstentions() -> None:
    """A predictor that declined contributes no calibration row."""
    bins = calibrate([_row(None, 1), _row(0.6, 1)], bins=5)

    assert sum(bin_.count for bin_ in bins) == 1, "only the answered row is counted"


def test_every_answered_row_lands_in_exactly_one_bin() -> None:
    """The bins partition the answered rows, so their counts sum to the scored rows."""
    rows = [_row(index / 100, index % 2) for index in range(101)]

    bins = calibrate(rows, bins=10)

    assert sum(bin_.count for bin_ in bins) == len(rows), "no row is lost or doubled"


def test_a_row_set_with_no_answer_has_no_score() -> None:
    """Nothing answered means no Brier, log loss or accuracy to report."""
    metrics = summarize([_row(None, 1)], clip=1e-6, threshold=0.5, bins=5)

    assert metrics.labelled == 1, "the row is still labelled"
    assert metrics.scored == 0, "but not scored"
    assert metrics.brier is None, "no Brier"
    assert metrics.log_loss is None, "no log loss"
    assert metrics.accuracy is None, "no accuracy"
    assert metrics.coverage == pytest.approx(0.0), "no coverage"


def test_summary_averages_the_answered_rows_only() -> None:
    """An abstention is counted but left out of every average."""
    rows = [_row(0.8, 1), _row(0.6, 0), _row(None, 1)]

    metrics = summarize(rows, clip=1e-6, threshold=0.5, bins=5)

    assert metrics.scored == 2, "two answers"
    assert metrics.abstentions == 1, "one abstention"
    assert metrics.brier == pytest.approx((0.04 + 0.36) / 2), "mean squared error"
    assert metrics.accuracy == pytest.approx(0.5), "one of the two decisions matched"


def test_summary_reports_the_mean_clipped_log_loss() -> None:
    """The mean log loss is the average of the per-row clipped losses.

    Each probability is the predicted chance of a hit, so a hit scores
    ``-log(p)`` and a miss scores ``-log(1 - p)``. The mean over the three
    answered rows is computed here from those values rather than restating the
    implementation.
    """
    rows = [_row(0.75, 1), _row(0.25, 0), _row(0.5, 1)]

    metrics = summarize(rows, clip=1e-6, threshold=0.5, bins=5)

    expected = (-math.log(0.75) - math.log(0.75) - math.log(0.5)) / 3
    assert metrics.log_loss == pytest.approx(expected), "mean of the row losses"


def test_the_clip_changes_a_certain_wrong_answer() -> None:
    """A zero probability for the observed outcome is clipped, not infinite.

    The row predicts a certain hit and the event missed, so an unclipped
    logarithm would be infinite. The clip sets the loss to ``-log(clip)``, and
    the mean over one answered row is that same value.
    """
    metrics = summarize([_row(0.0, 1)], clip=0.01, threshold=0.5, bins=5)

    assert metrics.log_loss == pytest.approx(-math.log(0.01)), "the clip bounds it"


@given(
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    st.integers(min_value=1, max_value=20),
)
@settings(max_examples=50, derandomize=True, deadline=None)
def test_one_answer_lands_in_exactly_one_of_any_number_of_bins(
    probability: float, bins: int
) -> None:
    """Whatever the bin count, one answered row is counted once and once only."""
    counts = [bin_.count for bin_ in calibrate([_row(probability, 1)], bins=bins)]

    assert len(counts) == bins, "every bin is reported, empty ones included"
    assert counts.count(1) == 1, "the row lands in exactly one bin"


@given(
    st.lists(
        st.one_of(
            st.none(),
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        ),
        max_size=12,
    ),
    st.integers(min_value=1, max_value=20),
)
@settings(max_examples=50, derandomize=True, deadline=None)
def test_abstentions_add_no_bin_count(
    probabilities: list[float | None], bins: int
) -> None:
    """The bins partition the answered rows, and only the answered rows."""
    rows = [
        _row(probability, index % 2) for index, probability in enumerate(probabilities)
    ]

    counts = [bin_.count for bin_ in calibrate(rows, bins=bins)]

    assert sum(counts) == sum(
        probability is not None for probability in probabilities
    ), "an abstention is left out of every bin"
