"""Block bootstrap over contiguous time blocks for paired score differences.

Adjacent compilations are correlated, so resampling single rows would
understate the uncertainty. Whole blocks of consecutive events are resampled
instead, which keeps each block's internal correlation intact.
"""

import dataclasses as dc
import math
import random
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.config import Config
    from buildlimits.predict.metrics import ScoredRow


@dc.dataclass(frozen=True, slots=True)
class Interval:
    """A paired mean difference and its percentile interval.

    Attributes
    ----------
    difference : float
        The observed mean difference, first predictor minus second.
    low : float
        Lower percentile of the bootstrap distribution.
    high : float
        Upper percentile of the bootstrap distribution.
    resamples : int
        Bootstrap replications drawn.
    block_size : int
        Consecutive rows per resampled block.
    rows : int
        Paired rows the difference was computed over.
    """

    difference: float
    low: float
    high: float
    resamples: int
    block_size: int
    rows: int

    def to_dict(self) -> dict[str, object]:
        """Return the report shape for one interval.

        Returns
        -------
        dict[str, object]
            The interval under stable keys.
        """
        return {
            "difference": self.difference,
            "low": self.low,
            "high": self.high,
            "resamples": self.resamples,
            "block_size": self.block_size,
            "rows": self.rows,
        }


def paired_differences(
    first: cabc.Sequence[ScoredRow], second: cabc.Sequence[ScoredRow]
) -> list[float]:
    """Return per-event Brier differences where both predictors answered.

    Returns
    -------
    list[float]
        First minus second, in event order.
    """
    differences: list[float] = []
    for left, right in zip(first, second, strict=True):
        if left.probability is None or right.probability is None:
            continue
        differences.append(
            (left.probability - left.outcome) ** 2
            - (right.probability - right.outcome) ** 2
        )
    return differences


def block_bootstrap(
    differences: cabc.Sequence[float], *, config: Config
) -> Interval | None:
    """Resample contiguous blocks and report a percentile interval.

    Returns
    -------
    Interval | None
        The observed difference and its interval, or None without rows.

    Examples
    --------
    A constant difference has a degenerate interval:

    >>> from buildlimits.predict.config import load_config
    >>> interval = block_bootstrap([0.05] * 40, config=load_config())
    >>> round(interval.difference, 2), round(interval.low, 2)
    (0.05, 0.05)
    """
    if not differences:
        return None
    stride = max(1, config.block_size)
    blocks = [
        list(differences[start : start + stride])
        for start in range(0, len(differences), stride)
    ]
    # ruff: ignore[suspicious-non-cryptographic-random-usage] - a seeded bootstrap
    # needs reproducible, not secret, draws.
    generator = random.Random(config.bootstrap_seed)
    means = [
        _resampled_mean(generator, blocks, len(differences))
        for _ in range(config.bootstrap_resamples)
    ]
    means.sort()
    return Interval(
        difference=sum(differences) / len(differences),
        low=_percentile(means, config.bootstrap_alpha / 2),
        high=_percentile(means, 1 - config.bootstrap_alpha / 2),
        resamples=config.bootstrap_resamples,
        block_size=stride,
        rows=len(differences),
    )


def _resampled_mean(
    generator: random.Random,
    blocks: cabc.Sequence[cabc.Sequence[float]],
    length: int,
) -> float:
    """Draw blocks with replacement until ``length`` rows are collected.

    The final block is truncated, so every replication averages exactly
    ``length`` rows. Drawing a fixed count of blocks would average fewer rows
    whenever the blocks do not divide the series evenly, which biases the
    interval.

    Returns
    -------
    float
        The mean of the resampled series.
    """
    total = 0.0
    seen = 0
    while seen < length:
        block = blocks[generator.randrange(len(blocks))]
        for value in block:
            if seen == length:
                break
            total += value
            seen += 1
    return total / length


def _percentile(ordered: cabc.Sequence[float], fraction: float) -> float:
    """Return a linearly interpolated percentile of a sorted sample.

    Returns
    -------
    float
        The percentile value.
    """
    if not ordered:
        return math.nan
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight
