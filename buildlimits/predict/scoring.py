"""Turn predictor answers into scored rows and metric blocks.

A row exists only where a label has arrived and the event could itself be
predicted. An event without a usable admission time has no answer to score, so
it is left out of the population entirely rather than counted as an
abstention: the predictor never had a chance to speak. Among the rows that
remain, an abstention is kept as an unscored row so the metrics report coverage
beside the score, and an unknown outcome never becomes a miss.

Nothing here reads a label that had not arrived at the row's place in the
stream, because the walk already holds one answer per event.
"""

import typing as typ

from buildlimits.predict.events import label_moment, scored_label
from buildlimits.predict.metrics import Metrics, ScoredRow, per_origin, summarize

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.predict.config import Config
    from buildlimits.predict.ensemble_runs import EnsembleRun
    from buildlimits.predict.events import Event
    from buildlimits.predict.harness import Walk
    from buildlimits.predict.metrics import Phase

PHASES: tuple[Phase, ...] = ("train", "held-out")


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
    """Score every full predictor on every row set it may honestly claim.

    A fixed ensemble's weights were fitted on the tuning labels, so it scores
    the held-out period only; every other predictor scores both phases.

    Returns
    -------
    dict[str, dict[str, Metrics]]
        Metrics per predictor.
    """
    blocks = {
        name: metric_block(rows_for(walk, name, series.probabilities), config)
        for name, series in walk.series.items()
    }
    blocks.update({
        name: metric_block(
            rows_for(walk, name, run.probabilities),
            config,
            phases=run.scored_phases,
        )
        for name, run in ensembles.items()
    })
    return blocks


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
                rows_for(walk, run.name, run.probabilities),
                config,
                phases=("held-out",),
            )["held-out"]
            for component, run in runs.items()
        }
        for mode, runs in ablations.items()
    }


def metric_block(
    rows: cabc.Sequence[ScoredRow],
    config: Config,
    *,
    phases: cabc.Sequence[Phase] = PHASES,
) -> dict[str, Metrics]:
    """Score one row set overall, per phase and per origin.

    Every entry is drawn from the requested phases only. The overall and
    per-origin scores are not a wider view of the same run: a fixed ensemble's
    weights were fitted on the tuning labels, so a tuning row is in-sample
    there. Reporting it under ``overall`` would publish a retrospective score
    as if it were a prediction, which is exactly what the phase restriction
    exists to prevent.

    Returns
    -------
    dict[str, Metrics]
        The overall score, each requested phase and every origin, all drawn
        from the same rows.
    """

    def scored(rows: cabc.Sequence[ScoredRow]) -> Metrics:
        """Summarize one row set with the pinned scoring parameters.

        Returns
        -------
        Metrics
            The counts and scores for exactly these rows.
        """
        return summarize(
            rows,
            clip=config.log_loss_clip,
            threshold=config.decision_threshold,
            bins=config.calibration_bins,
        )

    allowed = frozenset(phases)
    eligible = [row for row in rows if row.phase in allowed]
    block = {"overall": scored(eligible)}
    for phase in phases:
        block[phase] = scored([row for row in eligible if row.phase == phase])
    for origin, group in per_origin(eligible).items():
        block[f"origin:{origin}"] = scored(group)
    return block


def _scored_rows(
    walk: Walk, name: str, probabilities: cabc.Sequence[float | None]
) -> tuple[tuple[int, ScoredRow], ...]:
    """Return each predictable, labelled event's position and scored row.

    Returns
    -------
    tuple[tuple[int, ScoredRow], ...]
        Stream positions and rows, in stream order.
    """
    scored: list[tuple[int, ScoredRow]] = []
    for index, (event, phase, known, probability) in enumerate(
        zip(walk.events, walk.phases, walk.rows, probabilities, strict=True)
    ):
        if phase is None or known is None:
            continue
        label = scored_label(event)
        scored.append((
            index,
            ScoredRow(
                predictor=name,
                identity=event.identity,
                origin=event.origin,
                available_at=_verified_moment(event),
                probability=probability,
                outcome=1 if label == "hit" else 0,
                phase=phase,
            ),
        ))
    return tuple(scored)


def _verified_moment(event: Event) -> dt.datetime:
    """Return an event's verified label-availability moment.

    Only predictable events reach this helper: :func:`rows_for` skips every
    event the walk gave no row, and a row exists only for an event with a
    verified availability.

    Returns
    -------
    datetime
        The verified availability moment.

    Raises
    ------
    AssertionError
        If a predictable event has no verified availability, which would mean
        the walk and this scoring pass disagreed about the same stream.
    """
    moment = label_moment(event)
    if moment is None:
        msg = "a predictable event must carry a verified availability time"
        raise AssertionError(msg)
    return moment


def rows_for(
    walk: Walk,
    name: str,
    probabilities: cabc.Sequence[float | None],
    *,
    indices: cabc.Container[int] | None = None,
) -> tuple[ScoredRow, ...]:
    """Build one labelled row per predictable event for the given answers.

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
        predictable labelled event.

    Returns
    -------
    tuple[ScoredRow, ...]
        Rows in stream order, abstentions included.
    """
    return tuple(
        row
        for index, row in _scored_rows(walk, name, probabilities)
        if indices is None or index in indices
    )


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


def heldout_positions(
    walk: Walk, name: str, probabilities: cabc.Sequence[float | None]
) -> tuple[ScoredRow | None, ...]:
    """Return the held-out rows placed at the stream positions they occupy.

    Every position from the first to the last held-out row is represented. A
    position that was never predictable or never labelled stays empty, so a
    block formed over the result still spans the same moments of the stream;
    compacting those positions away would put rows that were many events apart
    into one block.

    Returns
    -------
    tuple[ScoredRow | None, ...]
        One entry per stream position in the held-out span, empty where no row
        exists.
    """
    held = {
        index: row
        for index, row in _scored_rows(walk, name, probabilities)
        if row.phase == "held-out"
    }
    if not held:
        return ()
    first, last = min(held), max(held)
    return tuple(held.get(index) for index in range(first, last + 1))
