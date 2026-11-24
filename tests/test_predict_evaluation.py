"""Ranking and verdict gates over the scored-row minimum."""

import dataclasses as dc
import datetime as dt
import typing as typ

import pytest
from predict_test_helpers import hand_event, predict_config

from buildlimits.predict.bootstrap import block_bootstrap
from buildlimits.predict.ensemble_runs import tuning_indices
from buildlimits.predict.evaluation import (
    STATUS_INSUFFICIENT_HELDOUT,
    STATUS_OK,
    _is_evidence,
    evaluate,
)
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.harness import Series, walk_forward
from buildlimits.predict.metrics import summarize
from buildlimits.predict.scoring import heldout_positions, rows_for
from buildlimits.predict.synthetic import STEP, identity, persistent

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import Origin
    from buildlimits.predict.config import Config
    from buildlimits.predict.evaluation import Evaluation
    from buildlimits.predict.harness import Walk
    from buildlimits.predict.metrics import Metrics

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
            label_observed_verified=True,
            completed_at=EPOCH + index * STEP,
            attempt=f"g{index}",
            supervisor_generation="test-generation",
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


def test_a_lone_block_of_evidence_cannot_meet_the_minimum() -> None:
    """Empty positions keep their place but cannot spread evidence over blocks.

    A hundred answered positions followed by nine hundred unanswered ones form
    ten windows, yet only the first holds a difference. The row minimum is met,
    so the block guard is the only thing that can see the difference came from
    a single stretch of the stream.
    """
    config = predict_config(block_size=100, minimum_scored_events=100)
    interval = block_bootstrap([-0.1] * 100 + [None] * 900, config=config)

    assert interval is not None, "the answered positions give an interval"
    assert interval.rows >= config.minimum_scored_events, "the row minimum is met"
    assert not _is_evidence(interval, config), "one evidenced block is not two"


def test_evidence_in_two_windows_meets_the_minimum() -> None:
    """Two answered windows with an empty one between them are two blocks."""
    config = predict_config(block_size=100, minimum_scored_events=100)
    interval = block_bootstrap(
        [-0.1] * 100 + [None] * 100 + [-0.1] * 100, config=config
    )

    assert interval is not None, "the answered positions give an interval"
    assert _is_evidence(interval, config), "two blocks carry evidence"


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
    events = _all_first_sightings(30)
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


def _late_outcomes() -> tuple[Walk, tuple[str, ...], tuple[int, ...]]:
    """Return the late walk, its single names and each event's outcome.

    Returns
    -------
    tuple[Walk, tuple[str, ...], tuple[int, ...]]
        The walk, the names of its singles in their fixed order, and 1 for a
        hit or 0 for a miss at each position.
    """
    walk = _late_held_out_admission()
    outcomes = tuple(1 if event.label == "hit" else 0 for event in walk.events)
    return walk, walk.components(), outcomes


def test_the_best_single_ignores_a_tuning_label_that_arrived_too_late() -> None:
    """A tuning label observed after the first held-out decision cannot decide it.

    The first single is right on the eight readable tuning rows and the second
    on the twelve late ones. Scored over every tuning row the second wins;
    scored over the rows the weight fit may read, the first does.
    """
    walk, names, outcomes = _late_outcomes()
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


def _tuning_metrics(walk: Walk, name: str, config: Config) -> Metrics:
    """Return one single's metrics over the rows a weight fit may read.

    Returns
    -------
    Metrics
        The score over the tuning rows eligible for the fit.
    """
    return summarize(
        rows_for(
            walk, name, walk.series[name].probabilities, indices=tuning_indices(walk)
        ),
        clip=config.log_loss_clip,
        threshold=config.decision_threshold,
        bins=config.calibration_bins,
    )


def test_the_best_single_is_ranked_on_the_rows_every_single_answered() -> None:
    """A single cannot win by abstaining on the tuning rows it would get wrong.

    ``honest`` answers all eight readable tuning rows and errs on the first
    two. ``picky`` abstains on exactly those two and errs once among the six it
    keeps, so on its own population it looks the better of the two. Ranking the
    singles on the rows both answered shows that advantage came from a
    different population rather than from a better answer, and leaves
    ``honest`` the winner.
    """
    walk, names, outcomes = _late_outcomes()
    honest = tuple(
        _answer(outcomes[index], is_correct=index >= 2) for index in range(30)
    )
    picky = tuple(
        None
        if index < 2 or index >= 8
        else _answer(outcomes[index], is_correct=index != 2)
        for index in range(30)
    )
    answered = _answered(walk, {0: honest, 1: picky})
    config = predict_config(train_fraction=20 / 30, minimum_scored_events=1)

    own = [_tuning_metrics(answered, names[position], config) for position in range(2)]
    result = evaluate(answered, config)

    assert own[0].brier is not None and own[1].brier is not None, "both were scored"
    assert own[1].brier < own[0].brier, (
        "on its own answered rows the abstaining single looks better"
    )
    assert result.best_single == names[0], "one shared population decides"


def test_an_unpredictable_event_keeps_its_place_in_the_held_out_span() -> None:
    """A position no predictor could answer still separates its neighbours.

    The middle event carries no admission time, so no row exists for it. Its
    position stays empty in the series rather than being compacted away,
    because a block of consecutive positions must not treat the events on its
    sides as neighbours.
    """
    events = (
        hand_event(
            0,
            admitted_at=EPOCH,
            observed_at=EPOCH + dt.timedelta(seconds=5),
            label="hit",
        ),
        hand_event(
            1,
            admitted_at=None,
            observed_at=EPOCH + dt.timedelta(seconds=10),
            label="miss",
        ),
        hand_event(
            2,
            admitted_at=EPOCH + dt.timedelta(seconds=15),
            observed_at=EPOCH + dt.timedelta(seconds=20),
            label="hit",
        ),
    )
    config = predict_config(train_fraction=0.0)
    walk = walk_forward(events, config)
    name = walk.components()[0]

    positions = heldout_positions(walk, name, walk.series[name].probabilities)

    assert len(positions) == 3, "the whole held-out span is represented"
    assert positions[1] is None, "the unpredictable event keeps its position"
    assert positions[0] is not None, "the first event was scored"
    assert positions[2] is not None, "the last event was scored"
