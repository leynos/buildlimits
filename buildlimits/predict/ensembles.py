"""Weighted consensus over the single predictors.

An ensemble never sees an event; it sees the probability each component gave
before that event's label was known. Equal and fixed ensembles carry constants,
while the adaptive ensemble moves its weights using only labels already
delivered, so the whole family stays causal.
"""

import dataclasses as dc
import math
import typing as typ

from buildlimits.predict.probability import clip_probability

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.config import Config

MODES = ("equal", "fixed", "adaptive")
_MIN_CURVATURE = 1e-12


@dc.dataclass(slots=True)
class Ensemble:
    """A weighted average of component probabilities.

    Attributes
    ----------
    name : str
        Stable identifier used in results.
    components : tuple[str, ...]
        Component predictor names, in weight order.
    weights : list[float]
        Current weights, renormalized over the components that answered.
    mode : str
        ``equal``, ``fixed`` or ``adaptive``.
    eta : float
        Adaptive learning rate; unused by the other modes.
    clip : float
        Probability clip shared with the component scores.
    """

    name: str
    components: tuple[str, ...]
    weights: list[float]
    mode: str
    eta: float
    clip: float

    def predict(self, probabilities: cabc.Sequence[float | None]) -> float | None:
        """Average the components that answered at this event.

        Returns
        -------
        float | None
            The weighted mean, or None when no component answered.
        """
        available = [
            (weight, probability)
            for weight, probability in zip(self.weights, probabilities, strict=True)
            if probability is not None
        ]
        mass = sum(weight for weight, _ in available)
        if not available or mass <= 0:
            return None
        mean = sum(weight * probability for weight, probability in available) / mass
        return clip_probability(mean, self.clip)

    def update(
        self, probabilities: cabc.Sequence[float | None], *, is_hit: bool
    ) -> None:
        """Move adaptive weights after a label; leave constant weights alone.

        A component that abstained is charged the average loss of the
        components that answered. Leaving its weight untouched would let it
        gain share purely by sitting out the events the others lost, and the
        renormalization below turns that into a larger weight when it returns.
        """
        if self.mode != "adaptive":
            return
        losses = [
            None
            if probability is None
            else _normalized_log_loss(probability, is_hit=is_hit, clip=self.clip)
            for probability in probabilities
        ]
        answered = [loss for loss in losses if loss is not None]
        if not answered:
            return
        shared = sum(answered) / len(answered)
        self.weights = [
            weight * math.exp(-self.eta * (shared if loss is None else loss))
            for weight, loss in zip(self.weights, losses, strict=True)
        ]
        total = sum(self.weights)
        if total > 0:
            self.weights = [weight / total for weight in self.weights]

    def without(self, index: int) -> Ensemble:
        """Return a copy with one component removed, for ablation.

        Returns
        -------
        Ensemble
            The same mode over the remaining components.
        """
        return dc.replace(
            self,
            name=f"{self.name}-without-{self.components[index]}",
            components=self.components[:index] + self.components[index + 1 :],
            weights=_normalized(self.weights[:index] + self.weights[index + 1 :]),
        )


def _normalized(weights: cabc.Sequence[float]) -> list[float]:
    """Scale weights to sum to one, or spread them evenly when all are zero.

    Returns
    -------
    list[float]
        The normalized weights.
    """
    total = sum(weights)
    if not weights:
        return []
    if total <= 0:
        return [1.0 / len(weights) for _ in weights]
    return [weight / total for weight in weights]


def _normalized_log_loss(probability: float, *, is_hit: bool, clip: float) -> float:
    """Return a log loss scaled into ``[0, 1]`` for exponentiated weights.

    Hedge assumes bounded losses. Dividing by ``log(1 / clip)`` maps the
    clipped log loss onto that range without changing which component wins.

    Returns
    -------
    float
        The normalized loss.
    """
    value = clip_probability(probability, clip)
    loss = -math.log(value if is_hit else 1.0 - value)
    return loss / math.log(1.0 / clip)


def fit_weights(
    rows: cabc.Sequence[cabc.Sequence[float | None]],
    outcomes: cabc.Sequence[float],
    *,
    config: Config,
) -> list[float]:
    """Fit non-negative weights summing to one on labelled rows only.

    The fit is a projected gradient descent on the log loss, so it is exactly
    reproducible and needs no library. Callers must pass training rows; the
    function has no way to reach any other label.

    Returns
    -------
    list[float]
        The fitted weights, in component order.

    Examples
    --------
    A component that always matches the outcome takes all the weight:

    >>> from buildlimits.predict.config import load_config
    >>> fit_weights([(1.0, 0.0)] * 8, [1.0] * 8, config=load_config())[0] > 0.9
    True
    """
    count = len(rows[0]) if rows else 0
    if count == 0:
        return []
    weights = [1.0 / count] * count
    if not outcomes:
        return weights
    for _ in range(config.weight_fit_steps):
        gradient = _log_loss_gradient(rows, outcomes, weights, config.log_loss_clip)
        weights = _project([
            weight - config.weight_fit_learning_rate * value
            for weight, value in zip(weights, gradient, strict=True)
        ])
    return weights


def _log_loss_gradient(
    rows: cabc.Sequence[cabc.Sequence[float | None]],
    outcomes: cabc.Sequence[float],
    weights: cabc.Sequence[float],
    clip: float,
) -> list[float]:
    """Return the mean gradient of the log loss over the rows.

    With ``m`` the clipped consensus probability, ``y`` the outcome and
    ``M`` the total weight of the components that answered, the derivative
    of the log loss with respect to a component's weight is
    ``(m - y) * p / (M * m * (1 - m))``.

    Returns
    -------
    list[float]
        One partial derivative per component.
    """
    gradient = [0.0] * len(weights)
    for row, outcome in zip(rows, outcomes, strict=True):
        available = [index for index, value in enumerate(row) if value is not None]
        mass = sum(weights[index] for index in available)
        if not available or mass <= 0:
            continue
        values = typ.cast("list[float]", [row[index] for index in available])
        mean = (
            sum(
                weights[index] * value
                for index, value in zip(available, values, strict=True)
            )
            / mass
        )
        probability = clip_probability(mean, clip)
        # The curvature vanishes at the extremes, where the clip has already
        # flattened the loss; the floor keeps the step finite there.
        curvature = max(probability * (1.0 - probability), _MIN_CURVATURE)
        error = probability - outcome
        for index, value in zip(available, values, strict=True):
            gradient[index] += error * value / (mass * curvature)
    return [value / len(outcomes) for value in gradient]


def _project(weights: cabc.Sequence[float]) -> list[float]:
    """Project weights onto the probability simplex.

    Returns
    -------
    list[float]
        The nearest non-negative weights summing to one.
    """
    ordered = sorted(weights, reverse=True)
    remainder = 0.0
    rho = 0
    for index, value in enumerate(ordered, start=1):
        remainder += value
        if value - (remainder - 1.0) / index > 0:
            rho = index
    if rho == 0:
        return [1.0 / len(weights) for _ in weights]
    threshold = (sum(ordered[:rho]) - 1.0) / rho
    return [max(value - threshold, 0.0) for value in weights]


def build_ensembles(
    components: cabc.Sequence[str],
    *,
    config: Config,
    weights: cabc.Sequence[float] | None = None,
) -> tuple[Ensemble, ...]:
    """Build the three consensus ensembles over the given components.

    Returns
    -------
    tuple[Ensemble, ...]
        Equal, fixed and adaptive ensembles, in that order.
    """
    names = tuple(components)
    equal = Ensemble(
        "ensemble-equal",
        names,
        _normalized([1.0] * len(names)),
        "equal",
        0.0,
        config.log_loss_clip,
    )
    usable = (
        [1.0] * len(names)
        if weights is None or len(weights) != len(names)
        else list(weights)
    )
    fitted = _normalized(usable)
    fixed = Ensemble(
        "ensemble-fixed", names, fitted, "fixed", 0.0, config.log_loss_clip
    )
    adaptive = Ensemble(
        "ensemble-adaptive",
        names,
        _normalized([1.0] * len(names)),
        "adaptive",
        config.hedge_eta,
        config.log_loss_clip,
    )
    return (equal, fixed, adaptive)
