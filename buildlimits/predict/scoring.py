"""Turn predictor answers into scored rows and metric blocks.

A row exists only where a label has arrived, so an abstention is kept as an
unscored row rather than dropped: the metrics then report coverage beside the
score, and an unknown outcome never becomes a miss. Nothing here reads a label
that had not arrived at the row's place in the stream, because the walk already
holds one answer per event.
"""

import typing as typ

from buildlimits.predict.events import scored_label
from buildlimits.predict.metrics import Metrics, ScoredRow, per_origin, summarize

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.config import Config
    from buildlimits.predict.ensemble_runs import EnsembleRun
    from buildlimits.predict.harness import Walk

PHASES = ("train", "held-out")


def series_map(
    walk: Walk, ensembles: cabc.Mapping[str, EnsembleRun]
) -> dict[str, cabc.Sequence[float | None]]:
    """Return every predictor's answers under its name.

    Returns
    -------
    dict[str, Sequence[float | None]]
        Singles first, then ensembles.
    """
    combined: dict[str, cabc.Sequence[float | None]] = {
        name: series.probabilities for name, series in walk.series.items()
    }
    combined.update({name: run.probabilities for name, run in ensembles.items()})
    return combined


def all_metrics(
    walk: Walk,
    ensembles: cabc.Mapping[str, EnsembleRun],
    config: Config,
) -> dict[str, dict[str, Metrics]]:
    """Score every full predictor on every row set.

    Returns
    -------
    dict[str, dict[str, Metrics]]
        Metrics per predictor.
    """
    return {
        name: metric_block(rows_for(walk, name, probabilities), config)
        for name, probabilities in series_map(walk, ensembles).items()
    }


def ablation_metrics(
    walk: Walk,
    ablations: cabc.Mapping[str, cabc.Mapping[str, EnsembleRun]],
    config: Config,
) -> dict[str, dict[str, Metrics]]:
    """Score each ablated ensemble on the held-out period.

    Returns
    -------
    dict[str, dict[str, Metrics]]
        Held-out metrics per mode and removed component.
    """
    return {
        mode: {
            component: metric_block(
                rows_for(walk, run.name, run.probabilities), config
            )["held-out"]
            for component, run in runs.items()
        }
        for mode, runs in ablations.items()
    }


def metric_block(rows: cabc.Sequence[ScoredRow], config: Config) -> dict[str, Metrics]:
    """Score one row set overall, per phase and per origin.

    Returns
    -------
    dict[str, Metrics]
        The overall score, both phases and every origin.
    """
    block = {
        "overall": summarize(
            rows,
            clip=config.log_loss_clip,
            threshold=config.decision_threshold,
            bins=config.calibration_bins,
        )
    }
    for phase in PHASES:
        block[phase] = summarize(
            [row for row in rows if row.phase == phase],
            clip=config.log_loss_clip,
            threshold=config.decision_threshold,
            bins=config.calibration_bins,
        )
    for origin, group in per_origin(rows).items():
        block[f"origin:{origin}"] = summarize(
            group,
            clip=config.log_loss_clip,
            threshold=config.decision_threshold,
            bins=config.calibration_bins,
        )
    return block


def rows_for(
    walk: Walk,
    name: str,
    probabilities: cabc.Sequence[float | None],
    *,
    indices: cabc.Container[int] | None = None,
) -> tuple[ScoredRow, ...]:
    """Build one labelled row per event for the given answers.

    Parameters
    ----------
    walk : Walk
        The scored stream.
    name : str
        The predictor the rows belong to.
    probabilities : Sequence[float | None]
        Its answer per event, in stream order.
    indices : Container[int] | None
        Restrict the rows to these stream positions, or None for every
        labelled event.

    Returns
    -------
    tuple[ScoredRow, ...]
        Rows in stream order, abstentions included.
    """
    rows: list[ScoredRow] = []
    for index, (event, phase, probability) in enumerate(
        zip(walk.events, walk.phases, probabilities, strict=True)
    ):
        if phase is None:
            continue
        if indices is not None and index not in indices:
            continue
        label = scored_label(event)
        rows.append(
            ScoredRow(
                predictor=name,
                identity=event.identity,
                origin=event.origin,
                observed_at=event.observed_at,
                probability=probability,
                outcome=1 if label == "hit" else 0,
                phase=phase,
            )
        )
    return tuple(rows)


def heldout_rows(
    walk: Walk, name: str, probabilities: cabc.Sequence[float | None]
) -> tuple[ScoredRow, ...]:
    """Return only the held-out rows for one predictor.

    Returns
    -------
    tuple[ScoredRow, ...]
        Held-out rows in stream order.
    """
    return tuple(
        row for row in rows_for(walk, name, probabilities) if row.phase == "held-out"
    )
