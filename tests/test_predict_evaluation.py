"""Ranking and verdict gates over the scored-row minimum."""

import dataclasses as dc
import typing as typ

from predict_test_helpers import predict_config

from buildlimits.predict.evaluation import (
    STATUS_INSUFFICIENT_HELDOUT,
    STATUS_OK,
    evaluate,
)
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.synthetic import STEP, identity, persistent

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import Origin
    from buildlimits.predict.evaluation import Evaluation

ORIGIN = typ.cast("Origin", "registry")


def _all_first_sightings(count: int) -> tuple[Event, ...]:
    """Return a stream in which no identity ever repeats."""
    return tuple(
        Event(
            identity(index),
            ORIGIN,
            None,
            EPOCH + index * STEP,
            "hit" if index % 2 else "miss",
            1.0,
        )
        for index in range(count)
    )


def _persistent_result(**changes: object) -> Evaluation:
    """Evaluate one persistent stream under the given config overrides.

    Returns
    -------
    Evaluation
        The scored stream and the verdict it supports.
    """
    config = predict_config(**changes)
    return evaluate(walk_forward(persistent(count=800, seed=3), config), config)


def test_a_predictor_below_the_scored_minimum_is_not_ranked() -> None:
    """Only a predictor scored over enough rows may lead the comparison."""
    config = predict_config()
    result = evaluate(walk_forward(_all_first_sightings(300), config), config)
    scored = {
        name: values["held-out"].scored for name, values in result.metrics.items()
    }
    top = max(scored.values())
    qualified = {name for name, count in scored.items() if count >= top}

    strict = dc.replace(result, minimum_scored_events=top)
    ranking = strict.ranking()

    assert len(ranking) == len(result.metrics), "every predictor still appears"
    assert set(ranking[: len(qualified)]) == qualified, "the qualified lead"
    assert all(scored[name] < top for name in ranking[len(qualified) :]), (
        "the rest follow in report order"
    )
    assert len(qualified) < len(result.metrics), "some predictor abstained often"


def test_a_real_improvement_is_named_in_the_verdict() -> None:
    """A predictor whose interval clears zero is reported as beating the rate."""
    result = _persistent_result()

    assert result.verdict.startswith("beats the base rate"), result.verdict
    assert "last-outcome" in result.verdict, "the persistent signal is named"


def test_the_verdict_needs_enough_paired_rows() -> None:
    """An interval over too few rows cannot support a claim of improvement."""
    result = _persistent_result(minimum_scored_events=100_000)

    assert result.status == STATUS_INSUFFICIENT_HELDOUT, "insufficient, not negative"
    assert result.verdict.startswith("insufficient held-out evidence"), result.verdict
    assert result.best_single is None, "no single predictor was eligible"


def test_the_verdict_needs_more_than_one_block() -> None:
    """Rows that form a single block show no sampling variability to judge.

    Every bootstrap replication would average the same values, so a
    zero-width interval could otherwise be read as a certain improvement.
    """
    result = _persistent_result(minimum_scored_events=300, block_size=500)

    assert result.status == STATUS_INSUFFICIENT_HELDOUT, "one block is not evidence"
    assert result.verdict.startswith("insufficient held-out evidence"), result.verdict


def test_one_block_withholds_a_claim_of_improvement() -> None:
    """The same stream beats the rate once it spans two blocks."""
    result = _persistent_result(block_size=200)

    assert result.status == STATUS_OK, "two blocks carry evidence"
    assert result.verdict.startswith("beats the base rate"), result.verdict
