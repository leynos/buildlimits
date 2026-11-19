"""Fit the consensus ensembles and walk each one across the event stream.

The fixed ensemble's weights are fitted on tuning labels that had already
arrived at the first held-out prediction, so every held-out answer is combined
with weights that could have existed then. Every ensemble then follows the same
admission schedule as the single predictors: at each admission moment the
labels observed strictly earlier are delivered, and only then is the moment's
row answered.

A fixed ensemble cannot score its own tuning rows: its weights were fitted on
those very labels, so a tuning score would be a retrospective fit rather than a
prediction. Those rows are excluded and the report states the limitation. The
equal and adaptive ensembles learn only from labels already delivered, so their
tuning rows are honest prequential scores.

Ablation restarts each ensemble from the weights it began with and simply drops
one component's column from the matrix. Starting from the final weights instead
would carry what the stream taught the adaptive ensemble about held-out labels
into the ablated comparison.
"""

import dataclasses as dc
import time
import typing as typ

from buildlimits.predict.ensembles import Ensemble, build_ensembles
from buildlimits.predict.events import scored_label
from buildlimits.predict.fitting import fit_weights
from buildlimits.predict.history import admission_schedule, released_labels

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.predict.config import Config
    from buildlimits.predict.events import Event
    from buildlimits.predict.harness import Walk
    from buildlimits.predict.metrics import Phase

BOTH_PHASES: tuple[Phase, ...] = ("train", "held-out")
HELDOUT_ONLY: tuple[Phase, ...] = ("held-out",)


@dc.dataclass(frozen=True, slots=True)
class EnsembleRun:
    """One ensemble's answers, final weights and prediction time.

    Attributes
    ----------
    name : str
        Stable identifier used in results.
    mode : str
        How the weights combine: ``fixed``, ``equal`` or ``adaptive``.
    probabilities : tuple[float | None, ...]
        One answer per event, in store order; None where no prediction was
        possible or the ensemble abstained.
    weights : tuple[float, ...]
        The weights in force after the last event.
    initial_weights : tuple[float, ...]
        The weights in force before the first event, which ablation restarts
        from.
    elapsed : tuple[float, ...]
        Seconds spent combining each event.
    scored_phases : tuple[Phase, ...]
        The phases whose rows are honest scores for this ensemble.
    """

    name: str
    mode: str
    probabilities: tuple[float | None, ...]
    weights: tuple[float, ...]
    initial_weights: tuple[float, ...]
    elapsed: tuple[float, ...]
    scored_phases: tuple[Phase, ...]


@dc.dataclass(frozen=True, slots=True)
class _Scoring:
    """How one ensemble run is scored and timed.

    Attributes
    ----------
    phases : tuple[Phase, ...]
        The phases whose rows are honest scores for this ensemble.
    timer : Callable[[], float]
        Monotonic seconds for each combined answer.
    """

    phases: tuple[Phase, ...]
    timer: cabc.Callable[[], float]


def run_ensembles(
    walk: Walk,
    config: Config,
    *,
    timer: cabc.Callable[[], float] = time.perf_counter,
) -> dict[str, EnsembleRun]:
    """Fit each ensemble and answer every event with it.

    Parameters
    ----------
    walk : Walk
        The replayed stream whose component answers and phases are combined.
    config : Config
        The pinned parameters, including the weight-fitting schedule.
    timer : Callable[[], float]
        Monotonic seconds, injected so a test can record known timings.

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
    return {
        ensemble.name: _run_ensemble(
            ensemble,
            matrix,
            walk,
            scoring=_Scoring(
                phases=HELDOUT_ONLY if ensemble.mode == "fixed" else BOTH_PHASES,
                timer=timer,
            ),
        )
        for ensemble in fitted
    }


def run_ablations(
    walk: Walk,
    config: Config,
    ensembles: cabc.Mapping[str, EnsembleRun],
    *,
    timer: cabc.Callable[[], float] = time.perf_counter,
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
    timer : Callable[[], float]
        Monotonic seconds, injected so a test can record known timings.

    Returns
    -------
    dict[str, dict[str, EnsembleRun]]
        Ablated runs keyed by the ensemble mode that was ablated.
    """
    components = walk.components()
    matrix = component_matrix(walk)
    results: dict[str, dict[str, EnsembleRun]] = {}
    for run in ensembles.values():
        base = _rebuild(run, components, config)
        results[run.mode] = {
            component: _run_ensemble(
                base.without(index),
                [row[:index] + row[index + 1 :] for row in matrix],
                walk,
                scoring=_Scoring(phases=run.scored_phases, timer=timer),
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
        One tuple per event; every column is None where no prediction was
        possible.
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

    Returns
    -------
    list[tuple[tuple[float | None, ...], float]]
        One component-answer row and outcome per usable tuning event.
    """
    return [
        (matrix[index], _outcome(walk.events[index])) for index in tuning_indices(walk)
    ]


def fit_cutoff(walk: Walk) -> dt.datetime | None:
    """Return the first held-out prediction moment, or None without one.

    The fitted weights are the history every held-out answer starts from, so
    they may read only tuning labels that had arrived by the earliest held-out
    admission. A stream with no held-out prediction has nothing to protect and
    lets the fit read every usable tuning row.

    Returns
    -------
    datetime | None
        The earliest held-out admission moment, or None.
    """
    moments = [
        row.predicted_at
        for index, row in enumerate(walk.rows)
        if row is not None
        and row.predicted_at is not None
        and walk.phases[index] == "held-out"
    ]
    return min(moments, default=None)


def tuning_indices(walk: Walk) -> tuple[int, ...]:
    """Return the tuning events a fixed weight fit may read.

    An event qualifies when it carries a tuning label, can itself be predicted,
    and its label had arrived by :func:`fit_cutoff`.

    Returns
    -------
    tuple[int, ...]
        Eligible tuning event indices, in stream order.
    """
    cutoff = fit_cutoff(walk)
    return tuple(
        index
        for index, phase in enumerate(walk.phases)
        if phase == "train"
        and walk.rows[index] is not None
        and (cutoff is None or walk.events[index].observed_at < cutoff)
    )


def _rebuild(run: EnsembleRun, components: tuple[str, ...], config: Config) -> Ensemble:
    """Recreate an ensemble from the weights it started the stream with.

    Returns
    -------
    Ensemble
        The ensemble in the state it was in before the first event.
    """
    return Ensemble(
        name=run.name,
        components=components,
        weights=list(run.initial_weights),
        mode=run.mode,
        eta=config.hedge_eta,
        clip=config.log_loss_clip,
        floor=config.hedge_weight_floor,
    )


def _run_ensemble(
    ensemble: Ensemble,
    matrix: cabc.Sequence[cabc.Sequence[float | None]],
    walk: Walk,
    *,
    scoring: _Scoring,
) -> EnsembleRun:
    """Answer one ensemble across the stream in admission order.

    Returns
    -------
    EnsembleRun
        Its answers, final weights and per-event timing.
    """
    initial_weights = tuple(ensemble.weights)
    probabilities: list[float | None] = [None] * len(walk.events)
    elapsed: list[float] = [0.0] * len(walk.events)
    released = released_labels(walk.events)
    for due, group in admission_schedule(walk.events, walk.moments, released):
        for index in due:
            _apply_label(ensemble, matrix[index], walk.events[index])
        for index in group:
            started = scoring.timer()
            probabilities[index] = ensemble.predict(matrix[index])
            elapsed[index] = scoring.timer() - started
    return EnsembleRun(
        name=ensemble.name,
        mode=ensemble.mode,
        probabilities=tuple(probabilities),
        weights=tuple(ensemble.weights),
        initial_weights=initial_weights,
        elapsed=tuple(elapsed),
        scored_phases=scoring.phases,
    )


def _apply_label(
    ensemble: Ensemble,
    row: cabc.Sequence[float | None],
    event: Event,
) -> None:
    """Deliver one released label to the ensemble."""
    label = scored_label(event)
    if label is not None:
        ensemble.update(row, is_hit=label == "hit")


def _outcome(event: Event) -> float:
    """Return the numeric outcome of one event.

    Returns
    -------
    float
        One for a hit, zero otherwise.
    """
    return 1.0 if scored_label(event) == "hit" else 0.0
