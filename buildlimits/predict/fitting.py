"""Fit the fixed ensemble's weights on tuning rows only.

The fit descends the same clipped log loss the report scores, and a step is
taken only when it does not raise that loss. Callers pass training rows; this
module has no way to reach a held-out label.

"""

import dataclasses as dc
import math
import typing as typ
from functools import partial

from buildlimits.predict.probability import clip_probability

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.config import Config

    type _Column = cabc.Sequence[float | None]

# The curvature vanishes where the clip has flattened the loss; the floor keeps
# the step finite there.
_MIN_CURVATURE = 1e-12
# A halved step below this cannot move a weight enough to matter, so the fit
# stops rather than shrinking it for ever.
_MIN_STEP = 1e-8


@dc.dataclass(frozen=True, slots=True)
class _Consensus:
    """One row's weighted mean and the terms its derivative needs.

    Attributes
    ----------
    mean : float
        The raw weighted mean, before clipping.
    mass : float
        Total weight of the components that answered.
    answered : tuple[tuple[int, float], ...]
        Component index and answer for each component that answered, in
        component order.
    """

    mean: float
    mass: float
    answered: tuple[tuple[int, float], ...]


def fit_weights(
    rows: cabc.Sequence[_Column],
    outcomes: cabc.Sequence[float],
    *,
    config: Config,
) -> list[float]:
    """Fit non-negative weights summing to one on labelled rows only.

    The fit is a projected gradient descent on the clipped log loss, so it is
    exactly reproducible and needs no library. The configured learning rate is
    the first step size tried; it is halved until the loss does not rise, which
    is what keeps a step taken beside a clipped probability from jumping past
    the minimum. The fit stops once no step of any size lowers the loss.

    Parameters
    ----------
    rows : Sequence[Sequence[float | None]]
        The component answers for each training row, in component order. A
        None entry marks a component that abstained at that row.
    outcomes : Sequence[float]
        The label for each training row, as 1.0 for a hit and 0.0 for a miss.
    config : Config
        The pinned parameters that set the step count, learning rate and clip.

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
    clip = config.log_loss_clip
    loss_at = partial(_mean_log_loss, rows, outcomes, clip=clip)
    for _ in range(config.weight_fit_steps):
        gradient = _log_loss_gradient(rows, outcomes, weights, clip)
        if not any(gradient):
            break
        proposal = _descend(loss_at, weights, gradient, config)
        if proposal is None:
            break
        weights = proposal
    return weights


def _descend(
    loss_at: cabc.Callable[[cabc.Sequence[float]], float],
    weights: cabc.Sequence[float],
    gradient: cabc.Sequence[float],
    config: Config,
) -> list[float] | None:
    """Return the largest halved step that does not raise the loss.

    A fixed step is fine where the loss is nearly quadratic, but next to a
    clipped probability the same step can carry the weights to the opposite
    corner of the simplex, where the loss is far worse than where it started.
    Halving keeps the direction and finds a size the loss accepts.

    Returns
    -------
    list[float] | None
        The improved weights, or None when no step size improved the loss,
        which means the fit has reached a point no small move improves.
    """
    loss = loss_at(weights)
    step = config.weight_fit_learning_rate
    while step >= _MIN_STEP:
        proposal = _project([
            weight - step * value
            for weight, value in zip(weights, gradient, strict=True)
        ])
        if loss_at(proposal) <= loss:
            return proposal
        step /= 2.0
    return None


def _mean_log_loss(
    rows: cabc.Sequence[_Column],
    outcomes: cabc.Sequence[float],
    weights: cabc.Sequence[float],
    clip: float,
) -> float:
    """Return the mean clipped log loss over the rows.

    This is the loss the report scores, so the fit optimizes the same
    quantity. A row whose raw mean the clip has flattened contributes the same
    loss whatever its weights are.

    Returns
    -------
    float
        The mean loss in nats.
    """
    total = 0.0
    for row, outcome in zip(rows, outcomes, strict=True):
        consensus = _consensus(row, weights)
        if consensus is None:
            continue
        probability = clip_probability(consensus.mean, clip)
        total -= outcome * math.log(probability) + (1.0 - outcome) * math.log(
            1.0 - probability
        )
    return total / len(outcomes)


def _log_loss_gradient(
    rows: cabc.Sequence[_Column],
    outcomes: cabc.Sequence[float],
    weights: cabc.Sequence[float],
    clip: float,
) -> list[float]:
    """Return the mean gradient of the clipped log loss over the rows.

    With ``m`` the clipped consensus probability, ``y`` the outcome, ``M`` the
    total weight of the components that answered and ``p`` the component's
    own answer, the consensus mean is a ratio whose denominator ``M`` also
    depends on the fitted weights. Its derivative with respect to a weight is
    therefore ``(p - mean) / M`` rather than ``p / M``, and the derivative of
    the log loss follows as ``(m - y) * (p - mean) / (M * m * (1 - m))``.
    Dropping the ``mean`` term would step against the normalization whenever a
    component abstains.

    Where the raw mean lies outside the clip interval the clip has flattened
    the loss, so the derivative there is zero. Reporting the interior formula
    evaluated at a clamped probability instead would claim a slope the scored
    loss does not have, and a fit trusting it steps off the corner it is
    standing on.

    Returns
    -------
    list[float]
        One partial derivative per component.
    """
    gradient = [0.0] * len(weights)
    for row, outcome in zip(rows, outcomes, strict=True):
        consensus = _consensus(row, weights)
        if consensus is None or not clip <= consensus.mean <= 1.0 - clip:
            continue
        probability = clip_probability(consensus.mean, clip)
        # The curvature vanishes at the extremes; the floor keeps the step
        # finite at the interval's edges.
        curvature = max(probability * (1.0 - probability), _MIN_CURVATURE)
        error = probability - outcome
        for index, value in consensus.answered:
            gradient[index] += (
                error * (value - consensus.mean) / (consensus.mass * curvature)
            )
    return [value / len(outcomes) for value in gradient]


def _consensus(row: _Column, weights: cabc.Sequence[float]) -> _Consensus | None:
    """Return a row's consensus mean, or None when nothing answered.

    Returns
    -------
    _Consensus | None
        The mean and the terms a derivative needs, or None when the row has
        nothing to average or the answering components carry no weight.
    """
    answered = tuple(
        (index, value) for index, value in enumerate(row) if value is not None
    )
    mass = sum(weights[index] for index, _ in answered)
    if not answered or mass <= 0:
        return None
    mean = sum(weights[index] * value for index, value in answered) / mass
    return _Consensus(mean=mean, mass=mass, answered=answered)


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
