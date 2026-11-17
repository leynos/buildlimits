"""Block bootstrap over contiguous time blocks for paired score differences.

Adjacent compilations are correlated, so resampling single rows would
understate the uncertainty. Whole blocks of consecutive events are resampled
instead, which keeps each block's internal correlation intact.

A block is a span of the original stream, not a run of answered rows. Removing
an abstention first would let one block join rows that were many events apart,
quietly treating unrelated moments as neighbours. Positions are therefore kept,
and a block averages only the answered rows that fall inside it.
"""

import dataclasses as dc
import math
import random
import typing as typ

from buildlimits.predict.metrics import brier_of

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
        Consecutive stream positions per resampled block.
    blocks : int
        Non-overlapping blocks that hold at least one answered position; fewer
        than two means the resampling cannot show any sampling variability.
        Empty blocks keep their positions in the resampling but do not count as
        independent evidence.
    rows : int
        Positions where both predictors answered and a difference was scored.
    positions : int
        Stream positions the series spans, answered or not.
    """

    difference: float
    low: float
    high: float
    resamples: int
    block_size: int
    blocks: int
    rows: int
    positions: int


def paired_differences(
    first: cabc.Sequence[ScoredRow | None],
    second: cabc.Sequence[ScoredRow | None],
) -> list[float | None]:
    """Return one Brier difference per stream position, gaps included.

    A position where either predictor abstained keeps its place as None, and so
    does a position where no row exists at all because the event could not be
    predicted or was never labelled. Dropping either would splice the rows on
    their sides into adjacent positions and let one block treat unrelated
    moments as neighbours.

    Parameters
    ----------
    first : Sequence[ScoredRow | None]
        Rows for the predictor being compared, in stream order, with None
        where it has no row.
    second : Sequence[ScoredRow | None]
        Rows for the predictor it is compared against, in the same order.

    Returns
    -------
    list[float | None]
        First minus second at each position, or None where either side has no
        answered row.

    Examples
    --------
    >>> from buildlimits.predict.events import EPOCH
    >>> from buildlimits.predict.metrics import ScoredRow
    >>> def row(p): return ScoredRow("p", "a" * 64, "git", EPOCH, p, 1, "train")
    >>> first = [row(0.9), row(None), row(0.1)]
    >>> second = [row(0.5), row(0.5), row(0.5)]
    >>> [None if d is None else round(d, 2) for d in paired_differences(first, second)]
    [-0.24, None, 0.56]
    >>> [None if d is None else round(d, 2) for d in paired_differences(
    ...     [row(0.9), None, row(0.1)], second
    ... )]
    [-0.24, None, 0.56]
    """
    return [
        None
        if left is None
        or right is None
        or left.probability is None
        or right.probability is None
        else brier_of(left.probability, left.outcome)
        - brier_of(right.probability, right.outcome)
        for left, right in zip(first, second, strict=True)
    ]


def block_bootstrap(
    differences: cabc.Sequence[float | None],
    *,
    config: Config,
    generator: random.Random | None = None,
) -> Interval | None:
    """Resample contiguous blocks and report a percentile interval.

    Each replication draws whole blocks from the original positions and
    averages the differences it found, so an unanswered position contributes
    no value but also does not shorten the block it sits in.

    Parameters
    ----------
    differences : Sequence[float | None]
        The paired difference at each stream position, None where unanswered.
    config : Config
        The pinned parameters: block size, resamples, level and seed.
    generator : random.Random | None
        The source of draws. None builds one from the configured seed, which
        is what makes a run reproducible; a caller may supply its own to
        replay a known sequence.

    Returns
    -------
    Interval | None
        The observed difference and its interval, or None without an answered
        position.

    Examples
    --------
    A constant difference has a degenerate interval:

    >>> from buildlimits.predict.config import load_config
    >>> interval = block_bootstrap([0.05] * 40, config=load_config())
    >>> round(interval.difference, 2), round(interval.low, 2)
    (0.05, 0.05)
    """
    observed = [value for value in differences if value is not None]
    if not observed:
        return None
    stride = max(1, config.block_size)
    size = len(differences)
    # ruff: ignore[suspicious-non-cryptographic-random-usage] - a seeded bootstrap
    # needs reproducible, not secret, draws.
    draws = generator if generator is not None else random.Random(config.bootstrap_seed)
    means: list[float] = []
    for _ in range(config.bootstrap_resamples):
        mean = _resampled_mean(draws, differences, stride, size)
        if mean is not None:
            means.append(mean)
    if not means:
        return None
    means.sort()
    return Interval(
        difference=sum(observed) / len(observed),
        low=_percentile(means, config.bootstrap_alpha / 2),
        high=_percentile(means, 1 - config.bootstrap_alpha / 2),
        resamples=config.bootstrap_resamples,
        block_size=stride,
        blocks=_evidence_blocks(differences, stride),
        rows=len(observed),
        positions=size,
    )


def _evidence_blocks(differences: cabc.Sequence[float | None], stride: int) -> int:
    """Return the non-overlapping windows that hold an answered position.

    Every stream position is kept so a resampled block still spans the same
    moments. The count reported here is narrower: a window of positions with no
    answered difference carries no evidence to judge, so it must not help an
    interval reach the minimum-block guard.

    Returns
    -------
    int
        Windows of ``stride`` positions, counted in stream order, that contain
        at least one answered position.
    """
    return sum(
        1
        for start in range(0, len(differences), stride)
        if any(value is not None for value in differences[start : start + stride])
    )


def _resampled_mean(
    generator: random.Random,
    differences: cabc.Sequence[float | None],
    stride: int,
    length: int,
) -> float | None:
    """Draw circular blocks with replacement and average their answers.

    Each block starts at a uniformly chosen position and wraps around the end of
    the series, so every block is the same length and every position is equally
    likely to be drawn. Forming a shorter final block from the leftover rows
    would sample it at the same rate as a full one and shift the resampled
    mean. A replication that lands entirely on unanswered positions has no mean
    to contribute and is reported as None.

    Returns
    -------
    float | None
        The mean of the answered differences drawn, or None when none was.
    """
    total = 0.0
    seen = 0
    used = 0
    size = len(differences)
    while seen < length:
        # The last block is cut to what the replication still needs. Drawing a
        # full block and discarding its tail would read the same positions but
        # build values no draw can use.
        remaining = min(stride, length - seen)
        block = circular_block(differences, generator.randrange(size), remaining)
        for value in block:
            if value is not None:
                total += value
                used += 1
            seen += 1
    return None if used == 0 else total / used


def circular_block(
    differences: cabc.Sequence[float | None], start: int, stride: int
) -> list[float | None]:
    """Return one full-length block, wrapping around the end of the series.

    Every block holds exactly ``stride`` positions, so no block is drawn at a
    shorter length than another. Wrapping also means each position appears in
    the same number of blocks, which is what keeps the resampled mean unbiased.

    Returns
    -------
    list[float | None]
        The block's values, beginning at ``start``; None where unanswered.

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
