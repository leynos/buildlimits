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
    blocks : int
        Non-overlapping blocks the series holds; fewer than two means the
        resampling cannot show any sampling variability.
    rows : int
        Paired rows the difference was computed over.
    """

    difference: float
    low: float
    high: float
    resamples: int
    block_size: int
    blocks: int
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
            "blocks": self.blocks,
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
    size = len(differences)
    # ruff: ignore[suspicious-non-cryptographic-random-usage] - a seeded bootstrap
    # needs reproducible, not secret, draws.
    generator = random.Random(config.bootstrap_seed)
    means = [
        _resampled_mean(generator, differences, stride, size)
        for _ in range(config.bootstrap_resamples)
    ]
    means.sort()
    return Interval(
        difference=sum(differences) / size,
        low=_percentile(means, config.bootstrap_alpha / 2),
        high=_percentile(means, 1 - config.bootstrap_alpha / 2),
        resamples=config.bootstrap_resamples,
        block_size=stride,
        blocks=math.ceil(size / stride),
        rows=size,
    )


def _resampled_mean(
    generator: random.Random,
    differences: cabc.Sequence[float],
    stride: int,
    length: int,
) -> float:
    """Draw circular blocks with replacement and average ``length`` values.

    Each block starts at a uniformly chosen row and wraps around the end of
    the series, so every block is the same length and every row is equally
    likely to be drawn. Forming a shorter final block from the leftover rows
    would sample it at the same rate as a full one and shift the resampled
    mean.

    Returns
    -------
    float
        The mean of the resampled series.
    """
    total = 0.0
    seen = 0
    size = len(differences)
    while seen < length:
        block = circular_block(differences, generator.randrange(size), stride)
        for value in block:
            if seen == length:
                break
            total += value
            seen += 1
    return total / length


def circular_block(
    differences: cabc.Sequence[float], start: int, stride: int
) -> list[float]:
    """Return one full-length block, wrapping around the end of the series.

    Every block holds exactly ``stride`` values, so no block is drawn at a
    shorter length than another. Wrapping also means each row appears in the
    same number of blocks, which is what keeps the resampled mean unbiased.

    Returns
    -------
    list[float]
        The block's values, beginning at ``start``.

    Examples
    --------
    >>> circular_block([1.0, 2.0, 3.0], 2, 2)
    [3.0, 1.0]
    """
    size = len(differences)
    return [differences[(start + offset) % size] for offset in range(stride)]


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
