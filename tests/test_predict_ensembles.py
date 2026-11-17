"""Consensus edge cases and the ablation restart contract."""

import pytest
from predict_test_helpers import predict_config

from buildlimits.predict.config import load_config
from buildlimits.predict.ensemble_runs import run_ablations, run_ensembles
from buildlimits.predict.ensembles import (
    Ensemble,
    _log_loss_gradient,
    fit_weights,
)
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.metrics import log_loss_of
from buildlimits.predict.synthetic import drift


def _adaptive(weights: list[float]) -> Ensemble:
    """Return an adaptive ensemble over two placeholder components."""
    return Ensemble(
        "ensemble-adaptive", ("steady", "wrong"), weights, "adaptive", 0.5, 1e-6
    )


def test_a_consensus_abstains_when_no_component_answers() -> None:
    """An ensemble cannot guess on its own."""
    assert _adaptive([0.5, 0.5]).predict((None, None)) is None, "no answer"


def test_zero_weights_give_no_consensus() -> None:
    """An ensemble whose weights have all been driven to zero abstains."""
    assert _adaptive([0.0, 0.0]).predict((0.9, 0.1)) is None, "no weight to spend"


def test_a_certain_consensus_is_clipped() -> None:
    """A consensus claiming certainty is clipped, so its log loss stays finite."""
    ensemble = _adaptive([1.0, 0.0])

    assert ensemble.predict((1.0, 0.0)) == pytest.approx(1.0 - 1e-6), "clipped"


def test_hedge_charges_an_abstainer_the_average_loss() -> None:
    """Sitting out a loss must not buy weight.

    The components that answered lost the same amount, so the abstainer is
    charged that same loss and the weights stay where they were. A component
    that paid nothing would gain share through the renormalization.
    """
    ensemble = _adaptive([0.5, 0.5])

    ensemble.update((0.9, None), is_hit=True)

    assert ensemble.weights == pytest.approx([0.5, 0.5]), "no free share"


def test_an_ensemble_that_heard_nothing_cannot_update() -> None:
    """A label with no component answers leaves the weights alone."""
    ensemble = _adaptive([0.4, 0.6])

    ensemble.update((None, None), is_hit=True)

    assert ensemble.weights == pytest.approx([0.4, 0.6]), "nothing to charge"


def test_an_ablation_without_any_weight_left_spreads_evenly() -> None:
    """Dropping a component from a spent ensemble still leaves a distribution."""
    ensemble = Ensemble(
        "ensemble-adaptive", ("a", "b", "c"), [0.0, 0.0, 0.0], "adaptive", 0.5, 1e-6
    )

    ablated = ensemble.without(0)

    assert ablated.weights == pytest.approx([0.5, 0.5]), "no weight means even"


def test_fit_weights_without_outcomes_keeps_even_weights() -> None:
    """A tuning period with no labelled rows cannot distinguish components."""
    weights = fit_weights([(0.9, 0.1)], [], config=load_config())

    assert weights == pytest.approx([0.5, 0.5]), "the uniform prior stands"


def test_fit_weights_without_rows_returns_no_weights() -> None:
    """No rows and no components give an empty weight vector."""
    assert fit_weights([], [], config=load_config()) == [], "nothing to fit"


def _mean_loss(
    rows: list[tuple[float | None, float | None]],
    outcomes: list[float],
    weights: list[float],
    *,
    clip: float,
) -> float:
    """Return the mean clipped log loss of a raw-weight consensus."""
    losses: list[float] = []
    for row, outcome in zip(rows, outcomes, strict=True):
        ensemble = Ensemble("probe", ("a", "b"), weights, "fixed", 0.0, clip)
        probability = ensemble.predict(row)
        if probability is not None:
            losses.append(log_loss_of(probability, int(outcome), clip=clip))
    return sum(losses) / len(losses)


@pytest.mark.parametrize("index", [0, 1])
def test_the_gradient_matches_a_finite_difference_under_abstention(index: int) -> None:
    """The consensus mean's normalization is part of its derivative.

    With one component abstaining, the remaining component's weight cancels
    out of the consensus mean, so its true partial derivative is zero. A
    gradient that omits the normalization term would step it anyway.
    """
    rows: list[tuple[float | None, float | None]] = [(0.8, None), (0.6, 0.3)]
    outcomes = [1.0, 0.0]
    weights = [0.4, 0.6]
    clip = 1e-6
    step = 1e-7
    up = list(weights)
    up[index] += step
    down = list(weights)
    down[index] -= step

    numeric = (
        _mean_loss(rows, outcomes, up, clip=clip)
        - _mean_loss(rows, outcomes, down, clip=clip)
    ) / (2 * step)
    gradient = _log_loss_gradient(rows, outcomes, weights, clip)

    assert gradient[index] == pytest.approx(numeric, abs=1e-4), "true derivative"


def test_ablation_restarts_each_ensemble_from_its_starting_weights() -> None:
    """An ablated run begins where the full run began, not where it ended."""
    config = predict_config()
    walk = walk_forward(
        drift(count=600, first_rate=0.9, second_rate=0.1, seed=5), config
    )
    ensembles = run_ensembles(walk, config)
    ablations = run_ablations(walk, config, ensembles)

    adaptive = ensembles["ensemble-adaptive"]
    assert adaptive.weights != pytest.approx(adaptive.initial_weights), (
        "the stream moved the full run's weights"
    )
    for run in ensembles.values():
        mode = run.name.removeprefix("ensemble-")
        for index, component in enumerate(walk.components()):
            kept = run.initial_weights[:index] + run.initial_weights[index + 1 :]
            expected = [weight / sum(kept) for weight in kept]
            assert ablations[mode][component].initial_weights == pytest.approx(
                expected
            ), f"{mode} without {component} restarted from its starting weights"
