"""Timing contracts for the fixed and adaptive ensemble weights.

The section covers when a weight may read a label: the fixed fit stops at the
first held-out admission, and an adaptive update reaches only answers given
after the label arrived.
"""

import dataclasses as dc
import datetime as dt
import itertools
import typing as typ

import pytest
from predict_test_helpers import hand_event, predict_config

from buildlimits.predict.ensemble_runs import (
    EnsembleRun,
    fit_cutoff,
    run_ensembles,
    tuning_indices,
)
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.scoring import heldout_rows, metric_block, rows_for
from buildlimits.predict.synthetic import drift, persistence_shift

if typ.TYPE_CHECKING:
    from buildlimits.predict.config import Config
    from buildlimits.predict.events import Label
    from buildlimits.predict.harness import Walk


def _flipped(label: str) -> str:
    """Return the opposite scorable label for one outcome."""
    return "miss" if label == "hit" else "hit"


def _eight_events(admissions: tuple[dt.datetime | None, ...]) -> tuple[Event, ...]:
    """Return eight events, each labelled at its own stream position."""
    return tuple(
        hand_event(
            index,
            observed_at=EPOCH + index * dt.timedelta(seconds=30),
            admitted_at=admissions[index],
            label="hit" if index % 2 else "miss",
        )
        for index in range(8)
    )


def _fitted_fixed_on_drift(count: int, seed: int) -> tuple[Walk, Config, EnsembleRun]:
    """Return a drifting walk, its config and the fixed ensemble fitted over it.

    Both drift scenarios arrange the same way: a stream whose hit rate falls
    part way through is walked forward, then the fixed weights are fitted on
    the tuning labels the split leaves readable.

    Returns
    -------
    tuple[Walk, Config, EnsembleRun]
        The walk, the parameters it was built with, and the fitted ensemble.
    """
    config = predict_config()
    walk = walk_forward(
        drift(count=count, first_rate=0.9, second_rate=0.1, seed=seed), config
    )
    return walk, config, run_ensembles(walk, config)["ensemble-fixed"]


def test_the_weight_fit_stops_at_the_first_held_out_admission() -> None:
    """A tuning label observed after the first held-out decision is too late.

    Event 3 is a tuning row by label time, but its label is observed at 90 s,
    after event 4 had already been admitted at 80 s. The fixed weights are the
    history every held-out answer starts from, so they cannot include it.
    """
    config = dc.replace(predict_config(), train_fraction=0.5)
    eight = [
        EPOCH + index * dt.timedelta(seconds=30) - dt.timedelta(seconds=5)
        for index in range(8)
    ]
    eight[4] = EPOCH + dt.timedelta(seconds=80)

    walk = walk_forward(_eight_events(tuple(eight)), config)

    assert walk.phases[:4] == ("train",) * 4, "the first four labels tune"
    assert walk.phases[4:] == ("held-out",) * 4, "the rest are held out"
    assert fit_cutoff(walk) == EPOCH + dt.timedelta(seconds=80), "the first decision"
    assert tuning_indices(walk) == (0, 1, 2), (
        "event 3's label arrived after the first held-out decision"
    )


def test_the_weight_fit_reads_nothing_when_every_tuning_label_arrives_late() -> None:
    """Late tuning labels cannot move the fixed weights at all.

    Every event is admitted at the epoch, so the first held-out decision is
    taken before any label has arrived. Flipping every tuning label must leave
    the fitted weights exactly where they started.
    """
    config = dc.replace(predict_config(), train_fraction=0.5)
    events = _eight_events((EPOCH,) * 8)
    flipped = tuple(
        dc.replace(event, label=typ.cast("Label", _flipped(event.label)))
        if index < 4
        else event
        for index, event in enumerate(events)
    )

    walk = walk_forward(events, config)
    before = run_ensembles(walk, config)["ensemble-fixed"].initial_weights
    after = run_ensembles(walk_forward(flipped, config), config)[
        "ensemble-fixed"
    ].initial_weights
    uniform = [1.0 / len(walk.components())] * len(walk.components())

    assert not tuning_indices(walk), "no tuning label had arrived in time"
    assert before == pytest.approx(uniform), "the uniform prior stands"
    assert after == pytest.approx(before), "flipping a withheld label changes nothing"
    assert flipped != events, "the perturbation changed at least one label"


def test_an_early_tuning_label_still_shapes_the_fixed_weights() -> None:
    """The cutoff withholds late labels, not every label.

    A drifting stream labels each event before the next is admitted, so every
    tuning label is readable at the first held-out decision and the fit moves
    off the uniform prior.
    """
    walk, _config, fixed = _fitted_fixed_on_drift(120, 5)
    uniform = [1.0 / len(fixed.initial_weights)] * len(fixed.initial_weights)

    assert len(tuning_indices(walk)) == walk.split.train_labelled, (
        "every tuning label arrived before the split"
    )
    assert fixed.initial_weights != pytest.approx(uniform), (
        "the fit reads the tuning labels it may read"
    )


MINUTE = dt.timedelta(minutes=1)


def test_a_late_label_cannot_reach_back_into_an_adaptive_answer() -> None:
    """The reviewer's case, for the adaptive weights as well as the predictors.

    The last request is admitted at minute 2 and its outcome is observed at
    minute 10. Flipping that outcome must not change the answer the adaptive
    ensemble gave at minute 2, because no weight update can have happened yet;
    it does change the final weights, which proves the flip was real.
    """
    events = (
        hand_event(0, admitted_at=EPOCH, observed_at=EPOCH + 30 * MINUTE, label="hit"),
        hand_event(
            1, admitted_at=EPOCH + MINUTE, observed_at=EPOCH + 10 * MINUTE, label="miss"
        ),
        hand_event(
            2,
            admitted_at=EPOCH + 2 * MINUTE,
            observed_at=EPOCH + 50 * MINUTE,
            label="hit",
        ),
        hand_event(
            3,
            admitted_at=EPOCH + 31 * MINUTE,
            observed_at=EPOCH + 60 * MINUTE,
            label="hit",
        ),
    )
    flipped = tuple(
        dc.replace(event, label=typ.cast("Label", _flipped(event.label)))
        if index == 1
        else event
        for index, event in enumerate(events)
    )
    config = predict_config()

    before = run_ensembles(walk_forward(events, config), config)["ensemble-adaptive"]
    after = run_ensembles(walk_forward(flipped, config), config)["ensemble-adaptive"]

    assert before.probabilities[2] is not None, "the minute-2 admission is answered"
    assert before.probabilities[2] == after.probabilities[2], (
        "the minute-10 label was not available at minute 2"
    )
    assert before.probabilities[:3] == after.probabilities[:3], (
        "no answer before the label arrived can read it"
    )
    assert before.probabilities[3] != after.probabilities[3], (
        "the flipped label does change the answer once it has arrived"
    )


def test_adaptive_weights_shift_towards_the_better_component_after_drift() -> None:
    """After the stream stops being random, last-outcome earns the weight.

    The first half draws every outcome afresh, so a smoothed posterior is a
    poor guide; the second half keeps each identity's outcome, so the posterior
    turns decisive and becomes the better guide. The adaptive weights must move
    towards ``beta-posterior`` past its even share, and the adaptive ensemble
    must beat the equal one on the held-out log loss.
    """
    config = predict_config()
    stream = persistence_shift(count=800, seed=2)
    walk = walk_forward(stream, config)
    runs = run_ensembles(walk, config)
    names = walk.components()
    beta = names.index("beta-posterior")
    adaptive = runs["ensemble-adaptive"]
    prefix = run_ensembles(walk_forward(stream[:400], config), config)[
        "ensemble-adaptive"
    ]

    def log_loss(run: EnsembleRun) -> float | None:
        """Return one run's held-out log loss."""
        return metric_block(heldout_rows(walk, run.name, run.probabilities), config)[
            "held-out"
        ].log_loss

    assert adaptive.weights[beta] > 1.0 / len(names), (
        "the better component holds more than an even share"
    )
    assert prefix.weights[beta] < 1.0 / len(names), (
        "the random half taught it nothing worth keeping"
    )
    adaptive_loss = log_loss(adaptive)
    equal_loss = log_loss(runs["ensemble-equal"])
    assert adaptive_loss is not None, "the adaptive ensemble was scored"
    assert equal_loss is not None, "the equal ensemble was scored"
    assert adaptive_loss < equal_loss, (
        "the shifted weights pay off on the held-out period"
    )


def test_a_fixed_ensemble_scores_only_rows_it_could_have_predicted() -> None:
    """The phase restriction covers the overall block, not only the phases.

    A fixed ensemble's weights were fitted on the tuning labels, so its tuning
    rows are in-sample. ``overall`` and the per-origin entries must report the
    same held-out population as ``held-out`` instead of folding those
    retrospective rows back in. The fixture holds tuning rows to exclude, so
    the equality is not vacuous.
    """
    walk, config, run = _fitted_fixed_on_drift(200, 7)
    rows = rows_for(walk, run.name, run.probabilities)

    block = metric_block(rows, config, phases=run.scored_phases)

    heldout = [row for row in rows if row.phase == "held-out"]
    origins = [key for key in block if key.startswith("origin:")]
    assert run.scored_phases == ("held-out",), "the fit makes tuning in-sample"
    assert any(row.phase == "train" for row in rows), "there are rows to exclude"
    assert block["overall"] == block["held-out"], "overall repeats the honest phase"
    assert block["overall"].labelled == len(heldout), "no tuning row survives"
    assert origins, "the fixture has at least one origin class"
    for key in origins:
        origin = key.removeprefix("origin:")
        assert block[key].labelled == sum(
            1 for row in heldout if row.origin == origin
        ), "each origin entry is drawn from the held-out rows only"


def test_the_ensemble_replay_times_itself_with_the_supplied_clock() -> None:
    """Every combined answer is charged the interval the injected clock gave."""
    step = 0.125
    ticks = itertools.count(0.0, step)
    config = dc.replace(predict_config(), train_fraction=0.5)
    eight = tuple(EPOCH + index * dt.timedelta(seconds=30) for index in range(8))

    walk = walk_forward(_eight_events(eight), config)
    runs = run_ensembles(walk, config, timer=lambda: next(ticks))

    for name, run in runs.items():
        answered = [value for value in run.elapsed if value]
        assert answered == pytest.approx([step] * len(answered)), (
            f"{name} timed its combination with the supplied clock"
        )
        assert len(answered) == sum(
            1 for value in run.probabilities if value is not None
        ), "every answered event was timed and no other"
