"""Probability arithmetic and the online logistic learner.

The log loss is only defined on ``(0, 1)``, so every learner and every
consensus clips its answer with :func:`clip_probability`. The logistic model
lives here rather than beside the other predictors because it is the only one
that needs a feature vector and a gradient step.
"""

import dataclasses as dc
import math
import typing as typ

from buildlimits.predict.encoding import LOGISTIC_WEIGHT_COUNT, logistic_features

if typ.TYPE_CHECKING:
    from buildlimits.predict.events import Features, ScoredLabel


def clip_probability(probability: float, clip: float) -> float:
    """Clamp a probability into the interval the log loss is defined on.

    Returns
    -------
    float
        ``probability`` limited to ``[clip, 1 - clip]``.
    """
    return min(max(probability, clip), 1.0 - clip)


def outcome_value(label: ScoredLabel) -> float:
    """Return 1.0 for a hit and 0.0 for a miss.

    Returns
    -------
    float
        The numeric outcome used by every learner.
    """
    return 1.0 if label == "hit" else 0.0


def _sigmoid(score: float) -> float:
    """Return the logistic function without overflowing either tail.

    Exponentiating ``-score`` directly underflows for large positive scores
    and overflows for large negative ones. Each branch keeps the exponent
    non-positive, which stays finite for every input.

    Returns
    -------
    float
        The probability in ``(0, 1)``.

    Examples
    --------
    >>> round(_sigmoid(0.0), 3)
    0.5
    >>> _sigmoid(-10_000.0)
    0.0
    """
    if score >= 0:
        return 1.0 / (1.0 + math.exp(-score))
    raised = math.exp(score)
    return raised / (1.0 + raised)


@dc.dataclass(slots=True)
class LogisticModel:
    """An online logistic regression over pre-admission features only.

    Attributes
    ----------
    name : str
        Stable identifier used in results.
    learning_rate : float
        Gradient step.
    l2 : float
        Penalty applied to every weight.
    clip : float
        Probability clip that keeps the gradient finite.
    weights : list[float]
        Model coefficients, all zero before the first update.
    """

    name: str
    learning_rate: float
    l2: float
    clip: float
    weights: list[float] = dc.field(
        default_factory=lambda: [0.0] * LOGISTIC_WEIGHT_COUNT
    )

    def predict(self, row: Features) -> float | None:
        """Return the logistic probability for this row's features.

        Returns
        -------
        float | None
            A probability; never None, because the bias term always applies.
        """
        score = sum(
            weight * value
            for weight, value in zip(self.weights, logistic_features(row), strict=True)
        )
        return clip_probability(_sigmoid(score), self.clip)

    def update(self, row: Features, label: ScoredLabel) -> None:
        """Take one gradient step on the delivered label."""
        features = logistic_features(row)
        probability = self.predict(row)
        if probability is None:
            return
        error = probability - outcome_value(label)
        for index, value in enumerate(features):
            penalty = self.l2 * self.weights[index]
            self.weights[index] -= self.learning_rate * (error * value + penalty)
