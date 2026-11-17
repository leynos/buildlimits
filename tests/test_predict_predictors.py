"""Known-answer contracts for the single predictors and the consensus family."""

import dataclasses as dc
import datetime as dt
import json
import typing as typ

import pytest

from buildlimits.predict.config import Config, load_config
from buildlimits.predict.dataset import Snapshot, Source
from buildlimits.predict.ensembles import Ensemble
from buildlimits.predict.evaluation import evaluate
from buildlimits.predict.events import EPOCH, Event, Features
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.predictors import (
    BaseRate,
    BetaPosterior,
    ExponentialRate,
    LastOutcome,
    RollingRate,
)
from buildlimits.predict.report import Provenance, build_report
from buildlimits.predict.synthetic import drift, identity, persistent, stationary

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import Origin


def _config(**changes: object) -> Config:
    """Return the pinned config with test-speed overrides."""
    base = dc.replace(
        load_config(), minimum_labelled_events=100, bootstrap_resamples=100
    )
    return dc.replace(base, **changes)


def _row(index: int, *, origin: str = "registry") -> Features:
    """Return one feature row for unit-level predictor tests."""
    return Features(
        identity=identity(index),
        origin=typ.cast("Origin", origin),
        observed_at=EPOCH,
        age_seconds=None,
        load=0,
    )


def _document(events: tuple[Event, ...], config: Config) -> dict[str, object]:
    """Evaluate one stream and return its deterministic document."""
    walk = walk_forward(events, config)
    created = dt.datetime(2026, 11, 17, tzinfo=dt.UTC)
    snapshot = Snapshot(
        created,
        Source("fixed-salt", created, 1, 1, 0),
        {},
        events,
        (),
    )
    return build_report(
        evaluate(walk, config),
        Provenance(snapshot, config, "abc123", "f" * 64),
    ).document


def test_stationary_stream_puts_the_base_rate_near_its_true_brier() -> None:
    """A stable 70% hit rate scores the base rate at about 0.21 Brier."""
    config = _config()
    walk = walk_forward(stationary(count=1200, rate=0.7, seed=11), config)

    result = evaluate(walk, config)

    heldout = result.metrics["base-rate"]["held-out"]
    assert heldout.brier == pytest.approx(0.21, abs=0.03), "known-answer Brier"
    assert heldout.coverage == pytest.approx(1.0), "the base rate never abstains"


def test_last_outcome_wins_on_a_persistent_stream() -> None:
    """When every identity repeats itself, the last outcome beats the base rate."""
    config = _config()
    walk = walk_forward(persistent(count=800, seed=3), config)

    result = evaluate(walk, config)

    last = result.metrics["last-outcome"]["held-out"]
    base = result.metrics["base-rate"]["held-out"]
    assert last.brier is not None and base.brier is not None, "both scored"
    assert last.brier < base.brier, "persistence is learnable"
    assert result.metrics["last-outcome"]["overall"].coverage < 1.0, (
        "a first sighting abstains"
    )


def test_unknown_outcomes_are_never_scored_as_misses() -> None:
    """An all-unknown stream leaves nothing to score at all."""
    config = _config()
    unknown = tuple(
        dc.replace(event, label="unknown")
        for event in stationary(count=120, rate=0.7, seed=2)
    )

    result = evaluate(walk_forward(unknown, config), config)

    for name in ("base-rate", "last-outcome", "logistic-model"):
        overall = result.metrics[name]["overall"]
        assert overall.labelled == 0, f"{name} saw no label"
        assert overall.scored == 0, f"{name} scored nothing"
        assert overall.brier is None, f"{name} has no Brier score"
    assert result.status == "insufficient data", "no label means no verdict"


def test_beta_posterior_follows_the_origin_rate_then_the_identity() -> None:
    """The prior is the origin rate, and an identity's own outcomes move it."""
    model = BetaPosterior("beta-posterior", 0.5, 8, 2.0)
    first, second, third = _row(1), _row(2), _row(3)

    assert model.predict(first) == pytest.approx(0.5), "cold-start prior"
    model.update(first, "hit")
    assert model.predict(second) == pytest.approx(1.0), "origin now always hits"
    model.update(second, "miss")
    assert model.predict(third) == pytest.approx(0.5), "origin rate is even"
    model.update(third, "miss")
    assert model.predict(third) == pytest.approx(2.0 / 9.0), (
        "one miss moves the posterior below the origin prior"
    )


def test_recency_models_abstain_until_they_have_a_label() -> None:
    """Last outcome, rolling rate and exponential rate abstain on a first sight."""
    row = _row(4)
    for predictor in (
        LastOutcome("last-outcome"),
        RollingRate("rolling-rate", 20),
        ExponentialRate("exponential-rate", 0.2),
    ):
        assert predictor.predict(row) is None, f"{predictor.name} abstains"


def test_base_rate_sequence_is_causal() -> None:
    """The base rate at event n uses only the labels delivered before it."""
    model = BaseRate("base-rate", 0.5)
    row = _row(0)

    assert model.predict(row) == pytest.approx(0.5), "cold start"
    model.update(row, "hit")
    assert model.predict(row) == pytest.approx(1.0), "one hit is a certain rate"
    model.update(row, "miss")
    assert model.predict(row) == pytest.approx(0.5), "two labels halve the rate"


def test_adaptive_ensemble_shifts_weight_towards_the_better_component() -> None:
    """Hedge moves weight to whichever component is losing less."""
    adaptive = Ensemble(
        "ensemble-adaptive",
        ("steady", "wrong"),
        [0.5, 0.5],
        "adaptive",
        0.5,
        1e-6,
    )
    for _ in range(100):
        adaptive.update((0.9, 0.1), is_hit=True)

    assert adaptive.weights[0] > 0.9, "the accurate component gains weight"
    assert adaptive.weights[1] < 0.1, "the inaccurate component loses weight"


def test_a_drifting_stream_moves_the_adaptive_ensemble_off_equal_weights() -> None:
    """When the hit rate changes halfway, Hedge no longer tracks the equal mix."""
    config = _config()
    stream = drift(count=600, first_rate=0.9, second_rate=0.1, seed=5)

    result = evaluate(walk_forward(stream, config), config)

    adaptive = result.ensembles["ensemble-adaptive"].weights
    equal = result.ensembles["ensemble-equal"].weights
    assert sum(adaptive) == pytest.approx(1.0), "weights stay a distribution"
    assert (
        sum(abs(mine - theirs) for mine, theirs in zip(adaptive, equal, strict=True))
        > 0.05
    ), "the drift moved weight away from the uniform allocation"


def test_fixed_weights_ignore_held_out_labels() -> None:
    """Mutating only held-out labels leaves the fitted weights unchanged."""
    config = _config()
    events = stationary(count=1200, rate=0.7, seed=8)
    walk = walk_forward(events, config)
    original = evaluate(walk, config)

    flipped = tuple(
        dc.replace(event, label="miss" if event.label == "hit" else "hit")
        if phase == "held-out"
        else event
        for event, phase in zip(events, walk.phases, strict=True)
    )
    mutated = evaluate(walk_forward(flipped, config), config)

    assert mutated.ensembles["ensemble-fixed"].weights == pytest.approx(
        original.ensembles["ensemble-fixed"].weights
    ), "tuning-period labels alone decide the weights"


def test_two_runs_produce_identical_results() -> None:
    """The same snapshot and config give a byte-identical result document."""
    config = _config()
    events = stationary(count=400, rate=0.6, seed=4)

    first = _document(events, config)
    second = _document(events, config)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True), (
        "deterministic document"
    )
