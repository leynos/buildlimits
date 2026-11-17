"""Ranking and verdict gates over the scored-row minimum."""

import dataclasses as dc
import typing as typ

import pytest
from predict_test_helpers import predict_config

from buildlimits.predict.evaluation import (
    STATUS_INSUFFICIENT_HELDOUT,
    STATUS_OK,
    evaluate,
)
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.harness import Series, walk_forward
from buildlimits.predict.synthetic import STEP, identity, persistent

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import Origin
    from buildlimits.predict.evaluation import Evaluation
    from buildlimits.predict.events import Label
    from buildlimits.predict.harness import Walk

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


def test_the_base_rate_comparisons_share_the_configured_level() -> None:
    """Several chances to beat the rate may not inflate the verdict."""
    config = predict_config()
    result = _persistent_result()

    family = len(result.against_base_rate)
    assert family == len(result.metrics) - 1, "every predictor but the rate is compared"
    assert result.verdict_alpha == pytest.approx(config.bootstrap_alpha / family), (
        "the configured level is divided across the family"
    )


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


def _two_tuning_batches() -> Walk:
    """Return a walk with one early tuning row and a large final tuning batch.

    Twenty tuning rows fill two batches: the first holds a single row, the
    second the 19 rows written together after it. Only the first is readable by
    a weight fit, because the second is the embargoed last tuning batch. Ten
    held-out rows follow so the comparison still has both sides.
    """
    first = Event(identity(0), ORIGIN, None, EPOCH, "hit", 1.0)
    together = tuple(
        Event(
            identity(1),
            ORIGIN,
            None,
            EPOCH + STEP,
            typ.cast("Label", "hit" if index % 2 else "miss"),
            1.0,
        )
        for index in range(19)
    )
    after = tuple(
        Event(identity(2 + index), ORIGIN, None, EPOCH + (2 + index) * STEP, "hit", 1.0)
        for index in range(10)
    )
    config = predict_config(train_fraction=20 / 30)
    return walk_forward((first, *together, *after), config)


def _answered(walk: Walk, answers: dict[int, tuple[float | None, ...]]) -> Walk:
    """Return ``walk`` with the named singles' answers replaced.

    Every other single abstains, so it cannot compete for the comparator.
    """
    abstained = tuple(None for _ in walk.events)
    series = {
        name: Series(
            name,
            tuple(answers.get(position, abstained)),
            tuple(0.0 for _ in walk.events),
        )
        for position, name in enumerate(walk.components())
    }
    return dc.replace(walk, series=series)


def test_the_best_single_ignores_the_embargoed_tuning_batch() -> None:
    """A batch no weight fit may read cannot decide the comparator.

    The first tuning row favours the first single and the 19 rows written
    together favour the second. Scored over every tuning row the second wins;
    scored over the rows the weight fit may read, the first does.
    """
    walk = _two_tuning_batches()
    names = walk.components()
    outcomes = [1 if event.label == "hit" else 0 for event in walk.events]
    favoured = (1.0, *(1.0 - outcome for outcome in outcomes[1:20]), *([0.5] * 10))
    disfavoured = (0.0, *(float(outcome) for outcome in outcomes[1:20]), *([0.5] * 10))
    answered = _answered(walk, {0: favoured, 1: disfavoured})

    result = evaluate(answered, predict_config(train_fraction=20 / 30))

    favoured_brier = result.metrics[names[0]]["train"].brier
    disfavoured_brier = result.metrics[names[1]]["train"].brier
    assert favoured_brier is not None, "the first single was scored"
    assert disfavoured_brier is not None, "the second single was scored"

    assert result.best_single == names[0], "the readable tuning row decides"
    assert disfavoured_brier < favoured_brier, (
        "scoring every tuning row would have picked the other single"
    )
