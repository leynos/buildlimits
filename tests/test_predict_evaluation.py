"""Ranking and verdict gates over the scored-row minimum."""

import dataclasses as dc
import typing as typ

from predict_test_helpers import predict_config

from buildlimits.predict.evaluation import evaluate
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.synthetic import STEP, identity, persistent

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import Origin

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
    config = predict_config()
    result = evaluate(walk_forward(persistent(count=800, seed=3), config), config)

    assert result.verdict.startswith("beats the base rate"), result.verdict
    assert "last-outcome" in result.verdict, "the persistent signal is named"


def test_the_verdict_needs_enough_paired_rows() -> None:
    """An interval over too few rows cannot support a claim of improvement."""
    config = predict_config(minimum_scored_events=100_000)
    result = evaluate(walk_forward(persistent(count=800, seed=3), config), config)

    assert result.verdict == "nothing beats the base rate on the held-out period", (
        "the claim is withheld, not softened"
    )
    assert result.best_single is None, "no single predictor was eligible"
