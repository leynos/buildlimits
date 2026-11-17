"""Ranking and verdict gates over the scored-row minimum."""

import dataclasses as dc
import datetime as dt
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
    """Return a stream in which no identity ever repeats.

    Each event is admitted five seconds before its own label, so every row can
    be predicted but no identity ever supplies a repeat for the predictors that
    need one.
    """
    return tuple(
        Event(
            identity(index),
            ORIGIN,
            EPOCH + index * STEP - dt.timedelta(seconds=5),
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

    def rank(name: str) -> tuple[bool, float]:
        """Sort a predictor by its score, placing a scoreless one last."""
        value = result.metrics[name]["held-out"].brier
        return (value is None, 0.0 if value is None else value)

    ordered = [
        name
        for name in sorted(result.metrics, key=rank)
        if result.metrics[name]["held-out"].brier is not None
    ]
    scoreless = [name for name in result.metrics if name not in ordered]

    # The worst-ranked predictor is given the best possible score on one row
    # too few: a leader by score alone, which the minimum must demote.
    demoted = ordered[-1]
    entry = result.metrics[demoted]
    short = dc.replace(entry["held-out"], scored=top - 1, brier=0.0)
    metrics = {**result.metrics, demoted: {**entry, "held-out": short}}
    demoting = dc.replace(result, metrics=metrics, minimum_scored_events=top)

    ranking = demoting.ranking()

    expected = [*ordered[:-1], demoted, *scoreless]
    assert list(ranking) == expected, "Brier order, qualified first, scoreless last"
    assert len(ranking) == len(result.metrics), "every predictor still appears"
    assert not demoting.meets_minimum(demoted), "one row short is not enough"
    assert ranking[0] != demoted, "the best score does not buy the lead"


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


def _late_held_out_admission() -> Walk:
    """Return a walk whose first held-out decision precedes half the tuning labels.

    Thirty labels split twenty tuning rows and ten held-out rows. The first
    held-out event is admitted after only eight tuning labels have arrived, so
    the last twelve tuning labels are observed too late for a weight fit to
    read.
    """
    events = tuple(
        Event(
            identity(index),
            ORIGIN,
            EPOCH + index * STEP - dt.timedelta(seconds=5),
            EPOCH + index * STEP,
            typ.cast("Label", "hit" if index % 2 else "miss"),
            1.0,
        )
        for index in range(30)
    )
    early = dc.replace(events[20], admitted_at=EPOCH + 7 * STEP + STEP / 2)
    config = predict_config(train_fraction=20 / 30)
    return walk_forward((*events[:20], early, *events[21:]), config)


def _answer(outcome: int, *, is_correct: bool) -> float:
    """Return a certain answer that is either right or wrong."""
    return float(outcome if is_correct else 1 - outcome)


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


def test_the_best_single_ignores_a_tuning_label_that_arrived_too_late() -> None:
    """A tuning label observed after the first held-out decision cannot decide it.

    The first single is right on the eight readable tuning rows and the second
    on the twelve late ones. Scored over every tuning row the second wins;
    scored over the rows the weight fit may read, the first does.
    """
    walk = _late_held_out_admission()
    names = walk.components()
    outcomes = [1 if event.label == "hit" else 0 for event in walk.events]
    early_right = tuple(
        _answer(outcomes[index], is_correct=index < 8) for index in range(30)
    )
    late_right = tuple(
        _answer(outcomes[index], is_correct=index >= 8) for index in range(30)
    )
    answered = _answered(walk, {0: early_right, 1: late_right})

    result = evaluate(
        answered,
        predict_config(train_fraction=20 / 30, minimum_scored_events=1),
    )

    early_brier = result.metrics[names[0]]["train"].brier
    late_brier = result.metrics[names[1]]["train"].brier
    assert early_brier is not None and late_brier is not None, "both were scored"
    assert late_brier < early_brier, "every tuning row would favour the second"

    assert result.best_single == names[0], "the readable tuning rows decide"
