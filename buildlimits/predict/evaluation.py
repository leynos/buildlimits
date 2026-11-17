"""Run the ensembles, score every predictor and choose the held-out verdict.

The fixed ensemble's weights are fitted on the tuning period alone. The
held-out period is then scored once, without any further influence on the
weights, so the comparison is what an operator would have seen in advance.
"""

import dataclasses as dc
import operator
import typing as typ

from buildlimits.predict.bootstrap import Interval, block_bootstrap, paired_differences
from buildlimits.predict.ensemble_runs import (
    EnsembleRun,
    run_ablations,
    run_ensembles,
)
from buildlimits.predict.events import scored_label
from buildlimits.predict.metrics import Metrics, ScoredRow, per_origin, summarize

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.config import Config
    from buildlimits.predict.harness import Walk

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient data"
_PHASES = ("train", "held-out")


@dc.dataclass(frozen=True, slots=True)
class Evaluation:
    """Every comparison the benchmark reports.

    Attributes
    ----------
    status : str
        ``ok`` or ``insufficient data``.
    walk : Walk
        The scored single-predictor stream.
    ensembles : dict[str, EnsembleRun]
        The three consensus ensembles.
    ablations : dict[str, dict[str, EnsembleRun]]
        Each ensemble with each component removed, keyed by mode and name.
    metrics : dict[str, dict[str, Metrics]]
        Per predictor, its overall, per-phase and per-origin scores.
    ablation_metrics : dict[str, dict[str, Metrics]]
        Each ensemble's held-out score with each component removed.
    against_base_rate : dict[str, Interval | None]
        Held-out Brier difference from the base rate, with its interval.
    best_single : str | None
        The single predictor with the best tuning-period Brier.
    against_best_single : dict[str, Interval | None]
        Held-out Brier difference between each ensemble and that predictor.
    verdict : str
        The plain-language outcome, including an honest negative one.
    """

    status: str
    walk: Walk
    ensembles: dict[str, EnsembleRun]
    ablations: dict[str, dict[str, EnsembleRun]]
    metrics: dict[str, dict[str, Metrics]]
    ablation_metrics: dict[str, dict[str, Metrics]]
    against_base_rate: dict[str, Interval | None]
    best_single: str | None
    against_best_single: dict[str, Interval | None]
    verdict: str

    def ranking(self) -> tuple[str, ...]:
        """Order every predictor by held-out Brier.

        Returns
        -------
        tuple[str, ...]
            Names with a held-out score, best first; unranked names follow.
        """
        scored = [
            (name, value)
            for name, value in self._heldout_brier().items()
            if value is not None
        ]
        ordered = [name for name, _ in sorted(scored, key=operator.itemgetter(1))]
        return tuple(ordered) + tuple(
            name for name in self.metrics if name not in ordered
        )

    def _heldout_brier(self) -> dict[str, float | None]:
        """Return each predictor's held-out Brier score.

        Returns
        -------
        dict[str, float | None]
            Brier per predictor, None where it answered nothing.
        """
        return {name: values["held-out"].brier for name, values in self.metrics.items()}


def evaluate(walk: Walk, config: Config) -> Evaluation:
    """Score the singles, run the ensembles and assemble the verdict.

    Returns
    -------
    Evaluation
        All metrics, intervals and the plain-language outcome.
    """
    ensembles = run_ensembles(walk, config)
    ablations = run_ablations(walk, config, ensembles)
    metrics = _all_metrics(walk, ensembles, config)
    ablation_metrics = _ablation_metrics(walk, ablations, config)
    compared = _compare(walk, ensembles, metrics, config)
    labelled = sum(1 for phase in walk.phases if phase is not None)
    status = (
        STATUS_OK if labelled >= config.minimum_labelled_events else STATUS_INSUFFICIENT
    )
    return Evaluation(
        status=status,
        walk=walk,
        ensembles=ensembles,
        ablations=ablations,
        metrics=metrics,
        ablation_metrics=ablation_metrics,
        against_base_rate=compared.against_base_rate,
        best_single=compared.best_single,
        against_best_single=compared.against_best_single,
        verdict=_verdict(status, labelled, compared.against_base_rate, config),
    )


def _compare(
    walk: Walk,
    ensembles: cabc.Mapping[str, EnsembleRun],
    metrics: cabc.Mapping[str, cabc.Mapping[str, Metrics]],
    config: Config,
) -> _Comparisons:
    """Compare every predictor with the base rate and the best single.

    Returns
    -------
    _Comparisons
        The held-out intervals and the tuning-period winner.
    """
    series = _series_map(walk, ensembles)
    comparisons = _Comparison(walk, series)
    best_single = _best_on_training(metrics, walk.components(), config)
    return _Comparisons(
        against_base_rate={
            name: comparisons.versus(name, "base-rate", config)
            for name in series
            if name != "base-rate"
        },
        best_single=best_single,
        against_best_single=(
            {}
            if best_single is None
            else {
                name: comparisons.versus(name, best_single, config)
                for name in ensembles
            }
        ),
    )


@dc.dataclass(frozen=True, slots=True)
class _Comparisons:
    """The held-out comparisons assembled for one evaluation.

    Attributes
    ----------
    against_base_rate : dict[str, Interval | None]
        Each predictor's Brier difference from the base rate.
    best_single : str | None
        The tuning-period winner among the singles.
    against_best_single : dict[str, Interval | None]
        Each ensemble's difference from that winner.
    """

    against_base_rate: dict[str, Interval | None]
    best_single: str | None
    against_best_single: dict[str, Interval | None]


def _series_map(
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


def _all_metrics(
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
        name: _metric_block(_rows_for(walk, name, probabilities), config)
        for name, probabilities in _series_map(walk, ensembles).items()
    }


def _ablation_metrics(
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
            component: _metric_block(
                _rows_for(walk, run.name, run.probabilities), config
            )["held-out"]
            for component, run in runs.items()
        }
        for mode, runs in ablations.items()
    }


def _metric_block(rows: cabc.Sequence[ScoredRow], config: Config) -> dict[str, Metrics]:
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
    for phase in _PHASES:
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


def _rows_for(
    walk: Walk, name: str, probabilities: cabc.Sequence[float | None]
) -> tuple[ScoredRow, ...]:
    """Build one labelled row per event for the given answers.

    Returns
    -------
    tuple[ScoredRow, ...]
        Rows in stream order, abstentions included.
    """
    rows: list[ScoredRow] = []
    for event, phase, probability in zip(
        walk.events, walk.phases, probabilities, strict=True
    ):
        if phase is None:
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


@dc.dataclass(frozen=True, slots=True)
class _Comparison:
    """The stream and every predictor's answers, ready for pairing.

    Attributes
    ----------
    walk : Walk
        The scored stream.
    series : Mapping[str, Sequence[float | None]]
        Answers per predictor.
    """

    walk: Walk
    series: cabc.Mapping[str, cabc.Sequence[float | None]]

    def versus(self, name: str, other: str, config: Config) -> Interval | None:
        """Compare two predictors over their common held-out rows.

        Returns
        -------
        Interval | None
            The Brier difference and its block-bootstrap interval.
        """
        ours = _heldout_rows(self.walk, name, self.series[name])
        theirs = _heldout_rows(self.walk, other, self.series[other])
        return block_bootstrap(paired_differences(ours, theirs), config=config)


def _heldout_rows(
    walk: Walk, name: str, probabilities: cabc.Sequence[float | None]
) -> tuple[ScoredRow, ...]:
    """Return only the held-out rows for one predictor.

    Returns
    -------
    tuple[ScoredRow, ...]
        Held-out rows in stream order.
    """
    return tuple(
        row for row in _rows_for(walk, name, probabilities) if row.phase == "held-out"
    )


def _best_on_training(
    metrics: cabc.Mapping[str, cabc.Mapping[str, Metrics]],
    singles: cabc.Sequence[str],
    config: Config,
) -> str | None:
    """Pick the single predictor with the lowest tuning-period Brier.

    Returns
    -------
    str | None
        The winner, or None when no single predictor scored enough rows.
    """
    scored = [
        (name, metrics[name]["train"].brier)
        for name in singles
        if metrics[name]["train"].scored >= config.minimum_scored_events
        and metrics[name]["train"].brier is not None
    ]
    return None if not scored else min(scored, key=operator.itemgetter(1))[0]


def _verdict(
    status: str,
    labelled: int,
    against_base: cabc.Mapping[str, Interval | None],
    config: Config,
) -> str:
    """State the outcome in one sentence, negative results included.

    Returns
    -------
    str
        The verdict.
    """
    if status != STATUS_OK:
        return (
            f"insufficient data: {labelled} labelled events, "
            f"{config.minimum_labelled_events} required"
        )
    improving = [
        name
        for name, interval in against_base.items()
        if interval is not None and interval.high < 0
    ]
    if not improving:
        return "nothing beats the base rate on the held-out period"
    return "beats the base rate on the held-out period: " + ", ".join(sorted(improving))
