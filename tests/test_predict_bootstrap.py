"""Block-bootstrap contracts for the paired score differences."""

import dataclasses as dc
import datetime as dt
import random
import typing as typ

import pytest
from predict_test_helpers import predict_config

from buildlimits.predict.bootstrap import (
    block_bootstrap,
    circular_block,
    paired_differences,
)
from buildlimits.predict.events import EPOCH
from buildlimits.predict.metrics import ScoredRow

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import Origin

ORIGIN = typ.cast("Origin", "registry")


def _row(probability: float | None, outcome: int) -> ScoredRow:
    """Return one scored row with a fixed identity and time."""
    return ScoredRow(
        "predictor", "a" * 64, ORIGIN, EPOCH, probability, outcome, "train"
    )


def _paired_stream(
    answered: dict[int, tuple[float, float]], *, side: int
) -> tuple[ScoredRow, ...]:
    """Return one side of a twelve-position stream, abstaining elsewhere.

    Returns
    -------
    tuple[ScoredRow, ...]
        A row per stream position; only answered positions carry a probability.
    """
    return tuple(
        _row(answered[index][side], 1) if index in answered else _row(None, 1)
        for index in range(12)
    )


def _rounded_pairs(
    ours: tuple[ScoredRow | None, ...], theirs: tuple[ScoredRow | None, ...]
) -> list[float | None]:
    """Return each paired difference rounded for an exact comparison.

    Returns
    -------
    list[float | None]
        One rounded value per stream position, with a gap left as ``None``.
    """
    return [
        None if value is None else round(value, 6)
        for value in paired_differences(ours, theirs)
    ]


def test_pairing_refuses_sequences_of_different_lengths() -> None:
    """A difference is only meaningful when both sides answered the same events."""
    with pytest.raises(ValueError, match="shorter"):
        paired_differences([_row(0.5, 1)], [])


def test_no_rows_give_no_interval() -> None:
    """A comparison with nothing to pair reports no interval at all."""
    assert block_bootstrap([], config=predict_config()) is None, "no rows, no interval"


def test_a_constant_difference_has_a_degenerate_interval() -> None:
    """Every replication averages exactly the rows it was given.

    The series is deliberately not a multiple of the block size. A replication
    that drew whole blocks and divided by the row count would overshoot the
    constant, so the interval would widen around a value that never varies.
    """
    interval = block_bootstrap([0.05] * 37, config=predict_config(block_size=10))

    assert interval is not None, "37 rows are enough"
    assert interval.low == pytest.approx(0.05), "lower percentile"
    assert interval.high == pytest.approx(0.05), "upper percentile"
    assert interval.rows == 37, "the row count is reported"
    assert interval.block_size == 10, "the stride is reported"


def test_every_drawn_block_has_the_same_length() -> None:
    """A block near the end wraps around instead of coming up short.

    Sampling a shorter final block at the same rate as a full one would shift
    the resampled mean, so the block that starts at the last row continues at
    the first.
    """
    assert circular_block([0.0, 0.0, 1.0], 2, 2) == [1.0, 0.0], "wraps around"
    assert circular_block([1.0, 2.0, 3.0], 0, 3) == [1.0, 2.0, 3.0], "whole series"


def test_a_single_block_is_reported_as_such() -> None:
    """A series shorter than one block cannot show sampling variability."""
    interval = block_bootstrap([0.04] * 30, config=predict_config(block_size=200))

    assert interval is not None, "30 rows still give an interval"
    assert interval.blocks == 1, "one block, so no width to report"
    assert interval.low == interval.high == pytest.approx(0.04), "degenerate"


def test_a_seeded_bootstrap_repeats_itself() -> None:
    """The same differences and seed give the same interval."""
    differences = [0.01 * (index % 7) - 0.02 for index in range(50)]

    first = block_bootstrap(differences, config=predict_config())
    second = block_bootstrap(differences, config=predict_config())

    assert first == second, "reproducible draws"


def test_paired_differences_keep_an_abstention_as_an_empty_position() -> None:
    """A row either side abstained on stays in the stream as a gap."""
    ours = (_row(0.9, 1), _row(None, 1), _row(0.2, 0), _row(0.5, 1))
    theirs = (_row(0.6, 1), _row(0.6, 1), _row(None, 0), _row(0.5, 1))

    differences = _rounded_pairs(ours, theirs)

    assert differences == [-0.15, None, None, 0.0], (
        "abstentions leave the position empty instead of closing the gap"
    )


def test_paired_differences_keep_a_position_with_no_row_as_a_gap() -> None:
    """A position neither predictor could answer still separates its neighbours."""
    ours: tuple[ScoredRow | None, ...] = (_row(0.9, 1), None, _row(0.2, 0))
    theirs: tuple[ScoredRow | None, ...] = (_row(0.6, 1), None, _row(0.6, 0))

    differences = _rounded_pairs(ours, theirs)

    assert differences == [-0.15, None, -0.32], (
        "the missing row leaves its position empty"
    )


def test_a_block_keeps_the_positions_it_spans_across_abstentions() -> None:
    """Blocks are drawn over stream positions, not over the answered rows.

    Only positions 0, 5 and 10 are answered, and a block of five positions can
    hold at most one of them. Dropping the abstentions first would put the three
    answers next to each other, and a block could then average two of them into
    a value the stream never had.
    """
    answered = {0: (0.0, 1.0), 5: (1.0, 0.0), 10: (0.0, 1.0)}
    ours = _paired_stream(answered, side=0)
    theirs = _paired_stream(answered, side=1)

    differences = paired_differences(ours, theirs)
    interval = block_bootstrap(
        differences, config=predict_config(block_size=5, bootstrap_resamples=200)
    )

    answered_positions = [
        index for index, value in enumerate(differences) if value is not None
    ]
    assert answered_positions == [0, 5, 10], (
        "the answers stay where the stream put them"
    )
    assert differences[0] == pytest.approx(1.0), "the first pair differs"
    assert differences[5] == pytest.approx(-1.0), (
        "the middle pair differs the other way"
    )
    assert differences[10] == pytest.approx(1.0), "the last pair differs again"
    assert interval is not None, "the answered positions give an interval"
    assert interval.rows == 3, "three rows were answered"
    assert interval.positions == 12, "over twelve stream positions"
    assert interval.difference == pytest.approx(1 / 3), "the mean of the answers"
    assert interval.low == pytest.approx(-1.0), "a block never averages across the gap"
    assert interval.high == pytest.approx(1.0), "only values the stream contains"


def test_an_interval_carries_its_provenance() -> None:
    """The interval reports the resampling it was built from."""
    config = predict_config(block_size=4, bootstrap_resamples=64)
    interval = block_bootstrap([0.02] * 16, config=config)

    assert interval is not None, "16 rows are enough"
    assert interval.resamples == 64, "replications"
    assert interval.difference == pytest.approx(0.02), "observed difference"


def test_the_percentile_brackets_the_observed_mean() -> None:
    """A varying series keeps its observed mean inside the reported interval."""
    differences = [0.1, -0.05, 0.2, -0.1, 0.0, 0.15, -0.2, 0.05] * 4
    interval = block_bootstrap(differences, config=predict_config(block_size=5))

    assert interval is not None, "32 rows are enough"
    assert interval.low <= interval.difference <= interval.high, "bracketed"
    assert interval.low < interval.high, "a varying series widens the interval"


def test_the_interval_shape_is_stable() -> None:
    """The report shape names every field a reader needs."""
    interval = block_bootstrap([0.03] * 12, config=predict_config())

    assert interval is not None, "12 rows are enough"
    document = interval.to_dict()
    for key in (
        "difference",
        "low",
        "high",
        "resamples",
        "block_size",
        "blocks",
        "rows",
        "positions",
    ):
        assert key in document, f"{key} is part of the stable shape"
    assert len(document) == 8, "no other key enters the shape"


def test_pairing_ignores_the_observed_time() -> None:
    """Pairing is positional, so rows are compared in stream order.

    The two streams answer differently, so a mismatch would show up in the
    differences; both calls carry the same answers and differ only in their
    timestamps.
    """
    first = (_row(0.2, 1), _row(0.8, 0))
    second = (_row(0.6, 1), _row(0.9, 0))
    shifted = tuple(
        dc.replace(row, observed_at=EPOCH + dt.timedelta(days=index))
        for index, row in enumerate(second)
    )

    differences = paired_differences(first, second)

    assert differences == pytest.approx([0.48, -0.17]), "first minus second"
    assert paired_differences(first, shifted) == pytest.approx(differences), (
        "timestamps do not enter the difference"
    )


def test_the_interval_narrows_as_the_sample_grows() -> None:
    """More rows must mean a tighter estimate, not a wider one.

    The same alternating series is resampled at two lengths. Independent rows
    (block size one) shrink the standard error with the square root of the
    sample, so the longer interval must be strictly narrower.
    """
    config = dc.replace(predict_config(), block_size=1, bootstrap_resamples=1_000)
    alternating = [0.25, -0.25]
    short = block_bootstrap(alternating * 20, config=config)
    long = block_bootstrap(alternating * 200, config=config)

    assert short is not None and long is not None, "both samples are resampled"
    assert long.high - long.low < short.high - short.low, "the estimate tightens"


def test_reversing_the_differences_mirrors_the_interval() -> None:
    """Negating every difference must reflect every percentile about zero.

    The draws depend only on the series length and the seed, so the mirrored
    run resamples the negated means in reverse order and its bounds are the
    negated bounds of the original.
    """
    config = dc.replace(predict_config(), block_size=4, bootstrap_resamples=500)
    differences = [0.4, -0.1, 0.2, 0.0, 0.3, -0.2, 0.1, 0.05]

    forward = block_bootstrap(differences, config=config)
    backward = block_bootstrap([-value for value in differences], config=config)

    assert forward is not None and backward is not None, "both runs resample"
    assert backward.difference == pytest.approx(-forward.difference), "the mean flips"
    assert backward.low == pytest.approx(-forward.high), "the upper tail mirrors"
    assert backward.high == pytest.approx(-forward.low), "and so does the lower"


def _seeded(seed: int) -> random.Random:
    """Return a seeded generator.

    Returns
    -------
    random.Random
        A generator whose draws replay exactly for the same seed.
    """
    return random.Random(seed)  # ruff: ignore[suspicious-non-cryptographic-random-usage] - a test replay needs reproducible, not secret, draws.


def test_an_injected_generator_makes_the_resampling_replayable() -> None:
    """The same injected draw sequence reproduces the same interval exactly."""
    differences = [0.1, None, -0.2, 0.3, None, 0.05] * 6
    config = dc.replace(
        predict_config(), bootstrap_resamples=20, block_size=5, bootstrap_seed=3
    )

    first = block_bootstrap(
        differences, config=config, generator=_seeded(config.bootstrap_seed)
    )
    second = block_bootstrap(
        differences, config=config, generator=_seeded(config.bootstrap_seed)
    )
    other = block_bootstrap(
        differences, config=config, generator=_seeded(config.bootstrap_seed + 1)
    )

    assert first == second, "the same draw sequence gives the same interval"
    assert first != other, "and a different sequence is genuinely different"
    assert first is not None and first.positions == len(differences), (
        "an abstention still occupies its stream position"
    )
