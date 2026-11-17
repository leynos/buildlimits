"""Denominator contracts for the fixed-weight fit's objective."""

import math

import pytest

from buildlimits.predict.fitting import _log_loss_gradient, _mean_log_loss
from buildlimits.predict.metrics import log_loss_of

CLIP = 1e-6
WEIGHTS = [0.5, 0.5]


def test_the_objective_averages_only_the_rows_that_answered() -> None:
    """A row no component answered is no evidence, not a perfect zero.

    Dividing the answered row's loss by every row would let a proposal that
    answered less look better than one that answered at all, so the fit could
    prefer weights that abstain on the whole tuning period.
    """
    loss = _mean_log_loss([(0.8, 0.6), (None, None)], [1.0, 0.0], WEIGHTS, CLIP)

    assert loss == pytest.approx(log_loss_of(0.7, 1, clip=CLIP)), (
        "the answered row is the whole of the mean"
    )


def test_the_objective_applies_the_clip_to_a_certainly_wrong_answer() -> None:
    """A consensus of zero that is a hit costs the clip, not an infinite score."""
    loss = _mean_log_loss([(0.0, 0.0)], [1.0], WEIGHTS, CLIP)

    assert loss == pytest.approx(-math.log(CLIP)), "the clip bounds the loss"


def test_the_objective_rejects_a_proposal_that_answers_nothing() -> None:
    """No answered row leaves no loss to compare, which must not read as zero."""
    loss = _mean_log_loss([(None, None)], [1.0], WEIGHTS, CLIP)

    assert loss == math.inf, "abstaining on every row is not a perfect fit"


def test_the_gradient_averages_only_the_rows_that_answered() -> None:
    """An unanswered row adds no slope, and does not dilute the slope there is."""
    answered = _log_loss_gradient([(0.8, 0.6)], [1.0], WEIGHTS, CLIP)
    padded = _log_loss_gradient([(0.8, 0.6), (None, None)], [1.0, 0.0], WEIGHTS, CLIP)

    assert padded == pytest.approx(answered), "the divisor counts answered rows"


def test_the_gradient_is_flat_when_nothing_answered() -> None:
    """With no answered row there is no direction for the fit to step in."""
    gradient = _log_loss_gradient([(None, None)], [1.0], WEIGHTS, CLIP)

    assert gradient == pytest.approx([0.0, 0.0]), "no slope, no step"
