"""Consensus edge cases and the ablation restart contract."""

import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from predict_test_helpers import predict_config

from buildlimits.predict.config_loader import load_config
from buildlimits.predict.ensemble_runs import (
    EnsembleRun,
    component_matrix,
    run_ablations,
    run_ensembles,
)
from buildlimits.predict.ensembles import Ensemble
from buildlimits.predict.fitting import _log_loss_gradient, _project, fit_weights
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.metrics import log_loss_of
from buildlimits.predict.synthetic import drift

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.harness import Walk


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


def test_a_negative_ablation_index_drops_the_last_component_once() -> None:
    """A position counted from the end removes one component, not a copy.

    Slicing the raw negative index keeps the head twice (``[:-1] + [0:]``), so
    the components would outnumber the weights and every later lookup would
    pair the wrong name with the wrong weight.
    """
    ensemble = Ensemble(
        "ensemble-equal", ("a", "b", "c"), [0.2, 0.3, 0.5], "equal", 0.5, 1e-6
    )

    ablated = ensemble.without(-1)

    assert ablated.components == ("a", "b"), "the last component is gone"
    assert ablated.weights == pytest.approx([0.4, 0.6]), "its weight is shared out"
    assert ablated.name == "ensemble-equal-without-c", "the name says which"


def test_a_long_losing_spell_cannot_eliminate_a_component() -> None:
    """A component outperformed for long enough must still win weight back.

    Multiplicative weights shrink toward zero, and once a normalized weight is
    exactly zero no later update can restore it. The configured floor keeps it
    positive and the recovery test proves it is not merely non-zero.
    """
    config = load_config()
    ensemble = Ensemble(
        "ensemble-adaptive",
        ("steady", "late"),
        [0.5, 0.5],
        "adaptive",
        config.hedge_eta,
        config.log_loss_clip,
        config.hedge_weight_floor,
    )

    for _ in range(400):
        ensemble.update((1.0, 0.0), is_hit=True)

    assert ensemble.weights[1] >= config.hedge_weight_floor, "floored, not zero"

    for _ in range(400):
        ensemble.update((0.0, 1.0), is_hit=True)

    assert ensemble.weights[1] > 0.4, "the weight is won back"


def test_fit_weights_without_outcomes_keeps_even_weights() -> None:
    """A tuning period with no labelled rows cannot distinguish components."""
    weights = fit_weights([(0.9, 0.1)], [], config=load_config())

    assert weights == pytest.approx([0.5, 0.5]), "the uniform prior stands"


def test_fit_weights_without_rows_returns_no_weights() -> None:
    """No rows and no components give an empty weight vector."""
    assert fit_weights([], [], config=load_config()) == [], "nothing to fit"


def test_the_fit_gives_more_weight_to_the_accurate_component() -> None:
    """A component that keeps being right gains the weight it earned."""
    rows = [(0.9, 0.2)] * 40
    outcomes = [1.0] * 40

    weights = fit_weights(rows, outcomes, config=load_config())

    assert weights[0] > 0.5, "the accurate component gains on the uniform share"
    assert weights[1] < 0.5, "and the inaccurate one loses"


@given(
    st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
        min_size=1,
        max_size=8,
    )
)
@settings(max_examples=50, derandomize=True, deadline=None)
def test_projected_weights_are_always_a_distribution(weights: list[float]) -> None:
    """The projection lands on the simplex whatever the gradient did."""
    projected = _project(weights)

    assert all(value >= 0.0 for value in projected), "no negative weight"
    assert sum(projected) == pytest.approx(1.0), "the mass stays at one"


def test_the_weight_fit_stops_short_of_a_step_that_raises_the_loss() -> None:
    """A fixed step beside the clip must not jump past the minimum.

    With 999 hits and one miss behind a pair of components, the loss is
    minimized where the correct component carries 0.999 of the weight. The
    pinned 0.5 step lands there from the uniform start; the step after it
    reaches the corner, where the clip leaves a loss nearly twice as large and
    the fit can no longer move away.
    """
    rows = [(1.0, 0.0)] * 1000
    outcomes = [1.0] * 999 + [0.0]
    config = load_config()

    weights = fit_weights(rows, outcomes, config=config)

    loss = _mean_loss(rows, outcomes, weights, clip=config.log_loss_clip)
    assert loss == pytest.approx(0.0079073, abs=1e-4), "near the optimum"
    assert weights[0] == pytest.approx(0.999, abs=5e-4), "not pinned to the corner"


def _mean_loss(
    rows: cabc.Sequence[tuple[float | None, float | None]],
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


@pytest.mark.parametrize(
    ("row", "outcome"),
    [
        pytest.param((0.01, 0.02), 1.0, id="below-the-clip"),
        pytest.param((0.99, 0.98), 0.0, id="above-the-clip"),
    ],
)
def test_the_gradient_is_zero_where_the_clip_flattens_the_loss(
    row: tuple[float, float], outcome: float
) -> None:
    """Outside the clip interval the scored loss is flat, so it has no slope.

    The interior formula evaluated at a clamped probability reports a slope the
    loss does not have, and a fit that trusted it would step away from the
    corner it is standing on.
    """
    weights = [0.5, 0.5]
    clip = 0.1
    step = 1e-7
    up = [weights[0] + step, weights[1] - step]
    down = [weights[0] - step, weights[1] + step]

    numeric = (
        _mean_loss([row], [outcome], up, clip=clip)
        - _mean_loss([row], [outcome], down, clip=clip)
    ) / (2 * step)

    assert _log_loss_gradient([row], [outcome], weights, clip) == pytest.approx([
        0.0,
        0.0,
    ]), "a flat loss has no slope"
    assert numeric == pytest.approx(0.0, abs=1e-6), "the scored loss agrees"


def test_the_gradient_keeps_the_interior_slope_at_the_clip_boundary() -> None:
    """A mean exactly on the clip keeps the slope the interior has.

    The flat rule applies strictly outside the interval; a boundary row is
    still moving the scored loss, so its derivative must not be dropped.
    """
    gradient = _log_loss_gradient([(0.0, 0.5)], [1.0], [0.5, 0.5], 0.25)

    assert gradient[0] == pytest.approx(1.0), "the slope survives the boundary"
    assert gradient[1] == pytest.approx(-1.0), "and mirrors across the mean"


def _drift_run() -> tuple[
    Walk, dict[str, EnsembleRun], dict[str, dict[str, EnsembleRun]]
]:
    """Return a drifting stream, its fitted ensembles and their ablations."""
    config = predict_config()
    walk = walk_forward(
        drift(count=600, first_rate=0.9, second_rate=0.1, seed=5), config
    )
    ensembles = run_ensembles(walk, config)
    return walk, ensembles, run_ablations(walk, config, ensembles)


def test_ablation_restarts_each_ensemble_from_its_starting_weights() -> None:
    """An ablated run begins where the full run began, not where it ended."""
    walk, ensembles, ablations = _drift_run()

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


def test_an_ablation_answers_differently_when_the_component_matters() -> None:
    """Dropping a component changes the consensus, not just its bookkeeping."""
    walk, ensembles, ablations = _drift_run()

    full = ensembles["ensemble-equal"].probabilities
    without = ablations["equal"][walk.components()[0]].probabilities

    assert len(without) == len(full), "the same events are answered"
    assert without != full, "the dropped component moved at least one answer"


def test_the_component_matrix_aligns_events_with_components() -> None:
    """Every event has one row, and every row has one answer per component."""
    config = predict_config()
    walk = walk_forward(
        drift(count=40, first_rate=0.9, second_rate=0.1, seed=5), config
    )

    matrix = component_matrix(walk)

    assert len(matrix) == len(walk.events), "one row per event"
    assert {len(row) for row in matrix} == {len(walk.components())}, (
        "one column per component"
    )
