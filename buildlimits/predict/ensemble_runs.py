"""Fit the consensus ensembles and walk each one across the event stream.

The fixed ensemble's weights are fitted on the tuning period alone, and stop
one tuning batch short of the first held-out batch: the fitted weights are the
history every later answer starts from, so they must end where an embargoed
prediction's history ends. Every ensemble is then replayed under the same
one-batch embargo as the single predictors: a batch's labels are applied only
after the next batch has been answered. That schedule advances on label
arrival, not on admission: an answer is combined when the replay reaches the
row's batch, so every label released before that batch is fair input, while the
row's age and load stay pinned to its admission. Ablation restarts each
ensemble from the weights it began with and simply drops one component's
column from the matrix. Starting from the final weights instead would carry
what the stream taught the adaptive ensemble about held-out labels into the
ablated comparison.
"""

import dataclasses as dc
import time
import typing as typ

from buildlimits.predict.ensembles import Ensemble, build_ensembles, fit_weights
from buildlimits.predict.events import scored_label

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.config import Config
    from buildlimits.predict.events import Event
    from buildlimits.predict.harness import Walk


@dc.dataclass(frozen=True, slots=True)
class EnsembleRun:
    """One ensemble's answers, final weights and prediction time.

    Attributes
    ----------
    name : str
        Stable identifier used in results.
    probabilities : tuple[float | None, ...]
        One answer per event, in stream order.
    weights : tuple[float, ...]
        The weights in force after the last event.
    initial_weights : tuple[float, ...]
        The weights in force before the first event, which ablation restarts
        from.
    elapsed : tuple[float, ...]
        Seconds spent combining each event.
    """

    name: str
    probabilities: tuple[float | None, ...]
    weights: tuple[float, ...]
    initial_weights: tuple[float, ...]
    elapsed: tuple[float, ...]


def run_ensembles(walk: Walk, config: Config) -> dict[str, EnsembleRun]:
    """Fit each ensemble and answer every event with it.

    Parameters
    ----------
    walk : Walk
        The replayed stream whose component answers and phases are combined.
    config : Config
        The pinned parameters, including the weight-fitting schedule.

    Returns
    -------
    dict[str, EnsembleRun]
        One run per consensus ensemble.
    """
    components = walk.components()
    matrix = component_matrix(walk)
    training = _tuning(walk, matrix)
    weights = fit_weights(
        [row for row, _ in training],
        [outcome for _, outcome in training],
        config=config,
    )
    fitted = build_ensembles(components, config=config, weights=weights)
    return {ensemble.name: _run_ensemble(ensemble, matrix, walk) for ensemble in fitted}


def run_ablations(
    walk: Walk, config: Config, ensembles: cabc.Mapping[str, EnsembleRun]
) -> dict[str, dict[str, EnsembleRun]]:
    """Repeat each ensemble with each component removed.

    Parameters
    ----------
    walk : Walk
        The replayed stream whose component answers are combined.
    config : Config
        The pinned parameters, including the weight-fitting schedule.
    ensembles : Mapping[str, EnsembleRun]
        The fitted runs; each ablation restarts from that run's
        ``initial_weights``, so removing a component removes its contribution
        rather than its already-adapted share.

    Returns
    -------
    dict[str, dict[str, EnsembleRun]]
        Ablated runs keyed by the ensemble mode that was ablated.
    """
    components = walk.components()
    matrix = component_matrix(walk)
    results: dict[str, dict[str, EnsembleRun]] = {}
    for run in ensembles.values():
        mode = run.name.removeprefix("ensemble-")
        base = _rebuild(run, components, config)
        results[mode] = {
            component: _run_ensemble(
                base.without(index),
                [row[:index] + row[index + 1 :] for row in matrix],
                walk,
            )
            for index, component in enumerate(components)
        }
    return results


def component_matrix(
    walk: Walk,
) -> list[tuple[float | None, ...]]:
    """Return each event's component answers, in component order.

    Parameters
    ----------
    walk : Walk
        The replayed stream whose series are read.

    Returns
    -------
    list[tuple[float | None, ...]]
        One tuple per event.
    """
    components = walk.components()
    return [
        tuple(walk.series[name].probabilities[index] for name in components)
        for index in range(len(walk.events))
    ]


def _tuning(
    walk: Walk, matrix: cabc.Sequence[tuple[float | None, ...]]
) -> list[tuple[tuple[float | None, ...], float]]:
    """Return the rows and outcomes a fixed weight fit may read.

    The last tuning batch is withheld for the same reason the replay withholds
    it: its labels may have been written after the first held-out builds
    started, so the weights every prediction starts from must not depend on
    them.

    Returns
    -------
    list[tuple[tuple[float | None, ...], float]]
        One component-answer row and outcome per remaining tuning event.
    """
    return [
        (matrix[index], _outcome(walk.events[index])) for index in tuning_indices(walk)
    ]


def tuning_indices(walk: Walk) -> tuple[int, ...]:
    """Return the tuning events a fixed weight fit may read.

    Every tuning row is eligible except those in the last tuning batch, which
    is withheld for the same reason the replay withholds it: its labels may
    have been written after the first held-out builds started.

    Returns
    -------
    tuple[int, ...]
        Eligible tuning event indices, in stream order.
    """
    withheld = _embargoed_tuning(walk)
    return tuple(
        index
        for index, phase in enumerate(walk.phases)
        if phase == "train" and index not in withheld
    )


def _embargoed_tuning(walk: Walk) -> frozenset[int]:
    """Return the tuning indices the weight fit must leave out.

    The tuning period ends at a label time, so the last batch holding a tuning
    row is the batch immediately before the first held-out one.

    Returns
    -------
    frozenset[int]
        Event indices in the last tuning batch, or empty without one.
    """
    last = max(
        (
            position
            for position, batch in enumerate(walk.batches)
            if any(walk.phases[index] == "train" for index in batch)
        ),
        default=None,
    )
    return frozenset() if last is None else frozenset(walk.batches[last])


def _rebuild(run: EnsembleRun, components: tuple[str, ...], config: Config) -> Ensemble:
    """Recreate an ensemble from the weights it started the stream with.

    Returns
    -------
    Ensemble
        The ensemble in the state it was in before the first event.
    """
    mode = run.name.removeprefix("ensemble-")
    return Ensemble(
        name=run.name,
        components=components,
        weights=list(run.initial_weights),
        mode=mode,
        eta=config.hedge_eta,
        clip=config.log_loss_clip,
        floor=config.hedge_weight_floor,
    )


def _run_ensemble(
    ensemble: Ensemble,
    matrix: cabc.Sequence[cabc.Sequence[float | None]],
    walk: Walk,
) -> EnsembleRun:
    """Answer one ensemble across the stream under the one-batch embargo.

    Returns
    -------
    EnsembleRun
        Its answers, final weights and per-event timing.
    """
    initial_weights = tuple(ensemble.weights)
    probabilities: list[float | None] = []
    elapsed: list[float] = []
    pending: tuple[int, ...] | None = None
    for batch in walk.batches:
        for index in batch:
            started = time.perf_counter()
            answer = ensemble.predict(matrix[index])
            elapsed.append(time.perf_counter() - started)
            probabilities.append(answer)
        if pending is not None:
            _apply_labels(ensemble, matrix, walk.events, pending)
        pending = batch
    return EnsembleRun(
        name=ensemble.name,
        probabilities=tuple(probabilities),
        weights=tuple(ensemble.weights),
        initial_weights=initial_weights,
        elapsed=tuple(elapsed),
    )


def _apply_labels(
    ensemble: Ensemble,
    matrix: cabc.Sequence[cabc.Sequence[float | None]],
    events: cabc.Sequence[Event],
    batch: cabc.Sequence[int],
) -> None:
    """Deliver one batch's eligible labels to the ensemble."""
    for index in batch:
        label = scored_label(events[index])
        if label is not None:
            ensemble.update(matrix[index], is_hit=label == "hit")


def _outcome(event: Event) -> float:
    """Return the numeric outcome of one event.

    Returns
    -------
    float
        One for a hit, zero otherwise.
    """
    return 1.0 if scored_label(event) == "hit" else 0.0
