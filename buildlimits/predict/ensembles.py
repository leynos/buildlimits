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
    floor : float
        Smallest normalized weight an adaptive update leaves a component.
        Without it a component that lost for long enough underflows to zero,
        and multiplying zero can never win the weight back.
    """

    name: str
    components: tuple[str, ...]
    weights: list[float]
    mode: str
    eta: float
    clip: float
    floor: float = 0.0

    def predict(self, probabilities: cabc.Sequence[float | None]) -> float | None:
        """Average the components that answered at this event.

        Parameters
        ----------
        probabilities : Sequence[float | None]
            One answer per component, in component order; None means that
            component abstained and is left out of the mean and the mass.

        Returns
        -------
        float | None
            The weighted mean, or None when no component answered or when
            every component that did answer carries zero weight, which leaves
            the mean undefined rather than zero.
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
        The floor keeps every component's weight positive, so a long losing
        spell cannot eliminate it for the rest of the stream.

        Parameters
        ----------
        probabilities : Sequence[float | None]
            One answer per component, in component order; None marks a
            component that abstained at this event.
        is_hit : bool
            The label that has just become known.
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
        self.weights = _floored(
            _normalized([
                weight * math.exp(-self.eta * (shared if loss is None else loss))
                for weight, loss in zip(self.weights, losses, strict=True)
            ]),
            floor=self.floor,
        )

    def without(self, index: int) -> Ensemble:
        """Return a copy with one component removed, for ablation.

        Parameters
        ----------
        index : int
            The position of the component to drop, in component order.

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


def _floored(weights: cabc.Sequence[float], *, floor: float) -> list[float]:
    """Raise every weight to a positive floor, keeping the total at one.

    A floor above the uniform share cannot be honoured by any distribution, so
    it is reduced to the uniform share first.

    Returns
    -------
    list[float]
        The weights, unchanged when the floor is disabled.
    """
    if not weights or floor <= 0:
        return list(weights)
    limit = min(floor, 1.0 / len(weights))
    lifted = [max(weight, limit) for weight in weights]
    slack = sum(weight - limit for weight in lifted)
    excess = sum(lifted) - 1.0
    if excess <= 0 or slack <= 0:
        return _normalized(lifted)
    return [weight - (weight - limit) * excess / slack for weight in lifted]


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
    scaled = loss / math.log(1.0 / clip)
    # ``1 - clip`` cannot be held exactly, so the far tail can divide out a
    # few parts per million above one. Hedge needs a bound rather than the
    # exact quotient, and the result is documented as lying in [0, 1].
    return min(scaled, 1.0)


def build_ensembles(
    components: cabc.Sequence[str],
    *,
    config: Config,
    weights: cabc.Sequence[float] | None = None,
) -> tuple[Ensemble, ...]:
    """Build the three consensus ensembles over the given components.

    Parameters
    ----------
    components : Sequence[str]
        The predictor names the ensembles combine, in component order.
    config : Config
        The pinned parameters, including the Hedge rate and the loss clip.
    weights : Sequence[float] | None
        The fitted fixed weights for the components. None, or a sequence of
        the wrong length, falls back to equal weights, so a stream with no
        training labels still yields a usable ensemble.

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
        config.hedge_weight_floor,
    )
    return (equal, fixed, adaptive)
