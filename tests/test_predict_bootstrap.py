"""Block-bootstrap contracts for the paired score differences."""

import dataclasses as dc
import datetime as dt
import typing as typ

import pytest
from predict_test_helpers import predict_config

from buildlimits.predict.bootstrap import (
    block_bootstrap,
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


def test_a_seeded_bootstrap_repeats_itself() -> None:
    """The same differences and seed give the same interval."""
    differences = [0.01 * (index % 7) - 0.02 for index in range(50)]

    first = block_bootstrap(differences, config=predict_config())
    second = block_bootstrap(differences, config=predict_config())

    assert first == second, "reproducible draws"


def test_paired_differences_skip_rows_where_either_side_abstained() -> None:
    """Only rows both predictors answered can be paired."""
    ours = (_row(0.9, 1), _row(None, 1), _row(0.2, 0), _row(0.5, 1))
    theirs = (_row(0.6, 1), _row(0.6, 1), _row(None, 0), _row(0.5, 1))

    differences = paired_differences(ours, theirs)

    assert differences == pytest.approx([
        (0.9 - 1) ** 2 - (0.6 - 1) ** 2,
        (0.5 - 1) ** 2 - (0.5 - 1) ** 2,
    ]), "abstentions are dropped from both sides"


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
    assert set(interval.to_dict()) == {
        "difference",
        "low",
        "high",
        "resamples",
        "block_size",
        "rows",
    }, "stable keys"


def test_pairing_ignores_the_observed_time() -> None:
    """Pairing is positional, so rows are compared in stream order."""
    rows = (_row(0.5, 1), _row(0.5, 0))
    shifted = tuple(
        dc.replace(row, observed_at=EPOCH + dt.timedelta(days=index))
        for index, row in enumerate(rows)
    )

    assert paired_differences(rows, rows) == pytest.approx([0.0, 0.0]), "no drift"
    assert paired_differences(shifted, shifted) == pytest.approx([0.0, 0.0]), (
        "timestamps do not enter the difference"
    )
