"""Direct contracts for the probability arithmetic and the logistic learner."""

import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from buildlimits.predict.events import EPOCH, Features
from buildlimits.predict.probability import LogisticModel, clip_probability

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import Origin

ORIGIN = typ.cast("Origin", "git")
FEATURES = Features("a" * 64, ORIGIN, EPOCH, 60.0, 2)


def _model(learning_rate: float = 0.05, clip: float = 1e-6) -> LogisticModel:
    """Return an unregularized learner with the given step and clip."""
    return LogisticModel("logistic-model", learning_rate, 0.0, clip)


def test_a_hit_raises_the_learned_probability() -> None:
    """One hit on the row's features moves the answer above a half.

    The model starts with every weight at zero, so this also pins the
    protocol answer before any label has arrived.
    """
    model = _model()
    before = model.predict(FEATURES)
    model.update(FEATURES, "hit")

    assert before == pytest.approx(0.5), "an unfitted model answers a half"
    after = model.predict(FEATURES)
    assert after is not None, "the bias term always answers"
    assert after > 0.5, "a hit raises the answer"


def test_a_miss_lowers_the_learned_probability() -> None:
    """Repeated misses drive the same row below a half."""
    model = _model()
    for _ in range(5):
        model.update(FEATURES, "miss")

    answer = model.predict(FEATURES)
    assert answer is not None, "the bias term always answers"
    assert answer < 0.5, "misses lower the answer"


def test_a_large_weight_cannot_escape_the_clip() -> None:
    """A saturated score still answers inside the interval log loss accepts."""
    model = _model()
    model.weights[0] = 10_000.0

    answer = model.predict(FEATURES)

    assert answer == pytest.approx(1.0 - 1e-6), "the upper clip holds"
    model.weights[0] = -10_000.0
    assert model.predict(FEATURES) == pytest.approx(1e-6), "the lower clip holds"


def test_the_clip_is_applied_to_a_probability_outside_the_unit_interval() -> None:
    """The clamp is the only place a learner answers, so it bounds both tails."""
    assert clip_probability(1.5, 1e-6) == pytest.approx(1.0 - 1e-6), "above one"
    assert clip_probability(-0.5, 1e-6) == pytest.approx(1e-6), "below zero"
    assert clip_probability(0.25, 1e-6) == pytest.approx(0.25), "inside the range"


@given(
    st.floats(allow_nan=False, allow_infinity=False),
    st.floats(min_value=1e-12, max_value=0.5, allow_nan=False),
)
@settings(max_examples=50, derandomize=True, deadline=None)
def test_clipping_keeps_every_answer_inside_the_interval(
    probability: float, clip: float
) -> None:
    """Any finite answer, however extreme, is clamped into the loss's domain."""
    value = clip_probability(probability, clip)

    assert clip <= value <= 1.0 - clip, "the clipped answer stays in range"
