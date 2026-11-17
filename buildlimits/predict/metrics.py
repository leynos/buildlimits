"""Scored rows and the metrics the benchmark reports for each predictor.

A row exists only where a label did arrive. A predictor that abstained still
owns the row, so coverage is visible; it simply contributes no Brier, log-loss
or accuracy term. An ``unknown`` outcome never becomes a row at all.
"""

import dataclasses as dc
import functools
import itertools
import math
import typing as typ

from buildlimits.predict.probability import clip_probability

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_record_format import Origin

type Phase = typ.Literal["train", "held-out"]


@dc.dataclass(frozen=True, slots=True)
class ScoredRow:
    """One predictor's answer for one labelled event.

    Attributes
    ----------
    predictor : str
        Which predictor answered.
    identity : str
        The hashed compilation identity, never used for scoring itself.
    origin : Origin
        The crate's origin class, used for the per-origin split.
    observed_at : datetime
        When the label became known.
    probability : float | None
        The answer, or None for an abstention.
    outcome : int
        One for a hit, zero for a miss.
    phase : Phase
        Whether the row falls in the tuning period or the held-out period.
    """

    predictor: str
    identity: str
    origin: Origin
    observed_at: dt.datetime
    probability: float | None
    outcome: int
    phase: Phase


@dc.dataclass(frozen=True, slots=True)
class CalibrationBin:
    """One fixed probability bin and what actually happened inside it.

    Attributes
    ----------
    lower : float
        Inclusive lower edge.
    upper : float
        Exclusive upper edge, except for the last bin.
    count : int
        Scored rows in the bin.
    mean_probability : float | None
        Average predicted probability, or None for an empty bin.
    observed_rate : float | None
        Share of hits, or None for an empty bin.
    """

    lower: float
    upper: float
    count: int
    mean_probability: float | None
    observed_rate: float | None

    def to_dict(self) -> dict[str, object]:
        """Return the report shape for one bin.

        Returns
        -------
        dict[str, object]
            The bin under stable keys.
        """
        return {
            "lower": self.lower,
            "upper": self.upper,
            "count": self.count,
            "mean_probability": self.mean_probability,
            "observed_rate": self.observed_rate,
        }


@dc.dataclass(frozen=True, slots=True)
class Metrics:
    """Every headline number for one predictor over one row set.

    Attributes
    ----------
    labelled : int
        Rows with a label, answered or not.
    scored : int
        Rows the predictor answered.
    abstentions : int
        Rows the predictor declined.
    brier : float | None
        Mean squared error, or None with no scored row.
    log_loss : float | None
        Mean clipped log loss, or None with no scored row.
    accuracy : float | None
        Share of thresholded decisions that matched, or None.
    calibration : tuple[CalibrationBin, ...]
        Fixed bins over every scored row.
    """

    labelled: int
    scored: int
    abstentions: int
    brier: float | None
    log_loss: float | None
    accuracy: float | None
    calibration: tuple[CalibrationBin, ...]

    @property
    def coverage(self) -> float:
        """Share of labelled rows the predictor answered.

        Returns
        -------
        float
            ``scored / labelled``, or zero when nothing was labelled.
        """
        return 0.0 if self.labelled == 0 else self.scored / self.labelled

    def to_dict(self) -> dict[str, object]:
        """Return the deterministic report shape.

        Returns
        -------
        dict[str, object]
            Every metric under stable keys.
        """
        return {
            "labelled": self.labelled,
            "scored": self.scored,
            "abstentions": self.abstentions,
            "coverage": self.coverage,
            "brier": self.brier,
            "log_loss": self.log_loss,
            "accuracy": self.accuracy,
            "calibration": [bin_.to_dict() for bin_ in self.calibration],
        }


def brier_of(probability: float, outcome: int) -> float:
    """Return the squared error of one answered row.

    Returns
    -------
    float
        ``(probability - outcome) ** 2``.

    Examples
    --------
    >>> round(brier_of(0.7, 1), 6)
    0.09
    """
    return (probability - outcome) ** 2


def log_loss_of(probability: float, outcome: int, *, clip: float) -> float:
    """Return the clipped Bernoulli log loss of one answered row.

    Returns
    -------
    float
        The loss with the predicted class probability kept above ``clip``.

    Examples
    --------
    >>> round(log_loss_of(0.7, 1, clip=1e-6), 6)
    0.356675
    """
    value = clip_probability(probability, clip)
    return -math.log(value if outcome == 1 else 1.0 - value)


def summarize(
    rows: cabc.Sequence[ScoredRow],
    *,
    clip: float,
    threshold: float,
    bins: int,
) -> Metrics:
    """Compute every headline metric over one row set.

    Returns
    -------
    Metrics
        The scores, coverage and calibration for the rows.
    """
    answered = [row for row in rows if row.probability is not None]
    pairs = [(typ.cast("float", row.probability), row.outcome) for row in answered]
    return Metrics(
        labelled=len(rows),
        scored=len(answered),
        abstentions=len(rows) - len(answered),
        brier=_mean_brier(pairs),
        log_loss=_mean_log_loss(pairs, clip),
        accuracy=_accuracy(pairs, threshold),
        calibration=calibrate(rows, bins=bins),
    )


def _mean_brier(pairs: cabc.Sequence[tuple[float, int]]) -> float | None:
    """Return the mean Brier score of the answered pairs.

    Returns
    -------
    float | None
        The mean, or None when nothing was answered.
    """
    if not pairs:
        return None
    return sum(itertools.starmap(brier_of, pairs)) / len(pairs)


def _mean_log_loss(
    pairs: cabc.Sequence[tuple[float, int]], clip: float
) -> float | None:
    """Return the mean clipped log loss of the answered pairs.

    Returns
    -------
    float | None
        The mean, or None when nothing was answered.
    """
    if not pairs:
        return None
    clipped = functools.partial(log_loss_of, clip=clip)
    return sum(itertools.starmap(clipped, pairs)) / len(pairs)


def _accuracy(
    pairs: cabc.Sequence[tuple[float, int]], threshold: float
) -> float | None:
    """Return the share of thresholded decisions that matched.

    Returns
    -------
    float | None
        The share, or None when nothing was answered.
    """
    if not pairs:
        return None
    correct = sum(
        1
        for probability, outcome in pairs
        if (probability >= threshold) == (outcome == 1)
    )
    return correct / len(pairs)


def calibrate(
    rows: cabc.Sequence[ScoredRow], *, bins: int
) -> tuple[CalibrationBin, ...]:
    """Group answered rows into fixed probability bins.

    Returns
    -------
    tuple[CalibrationBin, ...]
        Every bin, empty ones included, so counts are visible.
    """
    edges = [index / bins for index in range(bins + 1)]
    grouped: list[list[ScoredRow]] = [[] for _ in range(bins)]
    for row in rows:
        if row.probability is None:
            continue
        index = min(int(row.probability * bins), bins - 1)
        grouped[index].append(row)
    return tuple(
        CalibrationBin(
            lower=edges[index],
            upper=edges[index + 1],
            count=len(group),
            mean_probability=(
                None
                if not group
                else sum(typ.cast("float", row.probability) for row in group)
                / len(group)
            ),
            observed_rate=(
                None if not group else sum(row.outcome for row in group) / len(group)
            ),
        )
        for index, group in enumerate(grouped)
    )


def per_origin(rows: cabc.Sequence[ScoredRow]) -> dict[str, tuple[ScoredRow, ...]]:
    """Group rows by origin class.

    Returns
    -------
    dict[str, tuple[ScoredRow, ...]]
        Rows per origin, in a stable key order.
    """
    grouped: dict[str, list[ScoredRow]] = {}
    for row in rows:
        grouped.setdefault(row.origin, []).append(row)
    return {origin: tuple(grouped[origin]) for origin in sorted(grouped)}
