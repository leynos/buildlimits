"""The single predictors the benchmark compares.

Every predictor answers one question before the label is known and learns only
from labels already delivered. :class:`Predictor` is the whole protocol; the
ensembles in :mod:`buildlimits.predict.ensembles` combine predictors that obey
it without knowing what any of them are.
"""

import collections
import dataclasses as dc
import math
import typing as typ

from buildlimits.predict.encoding import LOGISTIC_WEIGHT_COUNT, logistic_features

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.config import Config
    from buildlimits.predict.events import Features, ScoredLabel


class Predictor(typ.Protocol):
    """One named probability source with causal updates.

    Both calls take their arguments positionally only, so an implementation
    that does not read the row may name the parameter to say so without
    breaking conformance.
    """

    name: str

    def predict(self, row: Features, /) -> float | None:
        """Return a probability in ``[0, 1]``, or None to abstain."""

    def update(self, row: Features, label: ScoredLabel, /) -> None:
        """Learn from a label that has already been delivered."""


def clip_probability(probability: float, clip: float) -> float:
    """Clamp a probability into the interval the log loss is defined on.

    Returns
    -------
    float
        ``probability`` limited to ``[clip, 1 - clip]``.
    """
    return min(max(probability, clip), 1.0 - clip)


def _value(label: ScoredLabel) -> float:
    """Return 1.0 for a hit and 0.0 for a miss.

    Returns
    -------
    float
        The numeric outcome used by every learner.
    """
    return 1.0 if label == "hit" else 0.0


@dc.dataclass(slots=True)
class BaseRate:
    """The global hit rate over every labelled event seen so far.

    Attributes
    ----------
    name : str
        Stable identifier used in results.
    cold_start : float
        Probability before any label has arrived.
    hits : int
        Scored hits so far.
    total : int
        Scored events so far.
    """

    name: str
    cold_start: float
    hits: int = 0
    total: int = 0

    def predict(self, row: Features) -> float | None:
        """Return the running hit rate.

        Returns
        -------
        float | None
            The rate, or the cold start before the first label.
        """
        del row
        return self.cold_start if self.total == 0 else self.hits / self.total

    def update(self, row: Features, label: ScoredLabel) -> None:
        """Account for one delivered label."""
        del row
        self.hits += label == "hit"
        self.total += 1


@dc.dataclass(slots=True)
class LastOutcome:
    """The most recent outcome for the same compilation identity.

    Attributes
    ----------
    name : str
        Stable identifier used in results.
    seen : dict[str, ScoredLabel]
        The last delivered label per identity.
    """

    name: str
    seen: dict[str, ScoredLabel] = dc.field(default_factory=dict)

    def predict(self, row: Features) -> float | None:
        """Repeat the identity's last label, abstaining for a first sighting.

        Returns
        -------
        float | None
            The repeated label, or None when the identity is new.
        """
        label = self.seen.get(row.identity)
        return None if label is None else _value(label)

    def update(self, row: Features, label: ScoredLabel) -> None:
        """Remember the identity's latest label."""
        self.seen[row.identity] = label


@dc.dataclass(slots=True)
class BetaPosterior:
    """A Beta posterior over an identity's last few outcomes.

    The prior is the identity's origin hit rate, so a crate from an origin that
    rarely hits starts pessimistic, and its own outcomes move it from there.

    Attributes
    ----------
    name : str
        Stable identifier used in results.
    cold_start : float
        Origin rate used before the origin has a label.
    window : int
        Outcomes kept per identity.
    prior_strength : float
        Total pseudo-count split around the origin rate.
    recent : dict[str, collections.deque[ScoredLabel]]
        Each identity's trailing outcomes.
    origin_hits : collections.Counter[str]
        Hits per origin.
    origin_total : collections.Counter[str]
        Labels per origin.
    """

    name: str
    cold_start: float
    window: int
    prior_strength: float
    recent: dict[str, collections.deque[ScoredLabel]] = dc.field(default_factory=dict)
    origin_hits: collections.Counter[str] = dc.field(
        default_factory=collections.Counter
    )
    origin_total: collections.Counter[str] = dc.field(
        default_factory=collections.Counter
    )

    def predict(self, row: Features) -> float | None:
        """Return the posterior mean for this identity and origin.

        Returns
        -------
        float | None
            A probability; never None, because the prior always exists.
        """
        rate = self._origin_rate(row.origin)
        label = self.recent.get(row.identity)
        hits = sum(1 for value in label or () if value == "hit")
        count = 0 if label is None else len(label)
        prior_hits = self.prior_strength * rate
        return (prior_hits + hits) / (self.prior_strength + count)

    def update(self, row: Features, label: ScoredLabel) -> None:
        """Append the label and refresh the origin rate."""
        series = self.recent.setdefault(
            row.identity, collections.deque(maxlen=self.window)
        )
        series.append(label)
        self.origin_total[row.origin] += 1
        self.origin_hits[row.origin] += label == "hit"

    def _origin_rate(self, origin: str) -> float:
        """Return the origin hit rate, or the cold start without labels.

        Returns
        -------
        float
            The origin's running hit rate.
        """
        total = self.origin_total[origin]
        return self.cold_start if total == 0 else self.origin_hits[origin] / total


@dc.dataclass(slots=True)
class RollingRate:
    """The hit rate over the last ``window`` labelled events anywhere.

    Attributes
    ----------
    name : str
        Stable identifier used in results.
    window : int
        Outcomes retained.
    recent : collections.deque[ScoredLabel]
        The trailing outcomes.
    """

    name: str
    window: int
    recent: collections.deque[ScoredLabel] = dc.field(default_factory=collections.deque)

    def __post_init__(self) -> None:
        """Give the window its fixed capacity."""
        self.recent = collections.deque(self.recent, maxlen=self.window)

    def predict(self, _row: Features) -> float | None:
        """Return the trailing hit rate.

        The protocol passes the row, and this rate is deliberately global
        rather than per identity, so the row is not read.

        Returns
        -------
        float | None
            The rate, or None before the first label.
        """
        if not self.recent:
            return None
        return sum(1 for value in self.recent if value == "hit") / len(self.recent)

    def update(self, _row: Features, label: ScoredLabel) -> None:
        """Append the label to the trailing window."""
        self.recent.append(label)


@dc.dataclass(slots=True)
class ExponentialRate:
    """An exponentially weighted hit rate over every labelled event.

    Each outcome is weighted by a factor that decays by ``1 - alpha`` per
    later label, and the rate is the weighted hits divided by the total
    weight. Dividing by that total normalises the early stream, where only
    a few outcomes carry weight, so the rate starts as a plain average of
    what has been seen rather than at the first outcome and decaying from
    there.

    Attributes
    ----------
    name : str
        Stable identifier used in results.
    alpha : float
        Weight of the newest outcome.
    hits : float
        Decayed weight of the hits.
    weight : float
        Decayed weight of every outcome.
    """

    name: str
    alpha: float
    hits: float = 0.0
    weight: float = 0.0

    def predict(self, _row: Features) -> float | None:
        """Return the weighted hit rate.

        The protocol passes the row, and this rate is deliberately global
        rather than per identity, so the row is not read.

        Returns
        -------
        float | None
            The rate, or None before the first label.
        """
        if not self.weight:
            # The mass only ever grows from exactly zero, so the empty case is
            # the one to answer with an abstention.
            return None
        return self.hits / self.weight

    def update(self, _row: Features, label: ScoredLabel) -> None:
        """Decay both masses and add the delivered outcome."""
        decay = 1.0 - self.alpha
        self.hits = self.alpha * _value(label) + decay * self.hits
        self.weight = self.alpha + decay * self.weight


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
        return clip_probability(1.0 / (1.0 + math.exp(-score)), self.clip)

    def update(self, row: Features, label: ScoredLabel) -> None:
        """Take one gradient step on the delivered label."""
        features = logistic_features(row)
        probability = self.predict(row)
        if probability is None:
            return
        error = probability - _value(label)
        for index, value in enumerate(features):
            penalty = self.l2 * self.weights[index]
            self.weights[index] -= self.learning_rate * (error * value + penalty)


def single_predictors(config: Config) -> tuple[Predictor, ...]:
    """Build the six single predictors from pinned parameters.

    Returns
    -------
    tuple[Predictor, ...]
        Base rate, last outcome, Beta posterior, rolling rate, exponential
        rate and the logistic feature model.
    """
    return (
        BaseRate("base-rate", config.cold_start),
        LastOutcome("last-outcome"),
        BetaPosterior(
            "beta-posterior",
            config.cold_start,
            config.beta_window,
            config.beta_prior_strength,
        ),
        RollingRate("rolling-rate", config.rolling_window),
        ExponentialRate("exponential-rate", config.ewma_alpha),
        LogisticModel(
            "logistic-model",
            config.logistic_learning_rate,
            config.logistic_l2,
            config.log_loss_clip,
        ),
    )


def update_all(
    predictors: cabc.Iterable[Predictor], row: Features, label: ScoredLabel
) -> None:
    """Deliver one label to every predictor."""
    for predictor in predictors:
        predictor.update(row, label)
