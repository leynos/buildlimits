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
from buildlimits.predict.scoring import (
    ablation_metrics,
    all_metrics,
    heldout_rows,
    series_map,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.config import Config
    from buildlimits.predict.harness import Walk
    from buildlimits.predict.metrics import Metrics

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient data"
STATUS_INSUFFICIENT_HELDOUT = "insufficient held-out evidence"
# One block resamples to itself, so its interval has no width to judge.
MINIMUM_BLOCKS = 2


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
    minimum_scored_events : int
        Held-out rows a predictor must score before it can be ranked.
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
    minimum_scored_events: int

    def ranking(self) -> tuple[str, ...]:
        """Order every predictor by held-out Brier.

        Only a predictor that scored at least ``minimum_scored_events``
        held-out rows is ranked: a Brier score over a handful of rows says
        nothing about which predictor is better. The remaining names follow
        the ranked ones, so every predictor still appears in the report.

        Returns
        -------
        tuple[str, ...]
            Names with a held-out score, best first; unranked names follow.
        """
        scored = [
            (name, value)
            for name, value in self._heldout_brier().items()
            if value is not None
            and self.metrics[name]["held-out"].scored >= self.minimum_scored_events
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

    Parameters
    ----------
    walk : Walk
        The replayed stream: every predictor's answers, the feature rows, the
        phases and the train/held-out split.
    config : Config
        The pinned parameters that decide thresholds, blocks and resamples.

    Returns
    -------
    Evaluation
        All metrics, intervals and the plain-language outcome.
    """
    ensembles = run_ensembles(walk, config)
    ablations = run_ablations(walk, config, ensembles)
    metrics = all_metrics(walk, ensembles, config)
    ablated = ablation_metrics(walk, ablations, config)
    compared = _compare(walk, ensembles, metrics, config)
    labelled = sum(1 for phase in walk.phases if phase is not None)
    status = _status(labelled, compared.against_base_rate, config)
    return Evaluation(
        status=status,
        walk=walk,
        ensembles=ensembles,
        ablations=ablations,
        metrics=metrics,
        ablation_metrics=ablated,
        against_base_rate=compared.against_base_rate,
        best_single=compared.best_single,
        against_best_single=compared.against_best_single,
        verdict=_verdict(status, labelled, compared.against_base_rate, config),
        minimum_scored_events=config.minimum_scored_events,
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
    series = series_map(walk, ensembles)
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
        ours = heldout_rows(self.walk, name, self.series[name])
        theirs = heldout_rows(self.walk, other, self.series[other])
        return block_bootstrap(paired_differences(ours, theirs), config=config)


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


def _status(
    labelled: int,
    against_base: cabc.Mapping[str, Interval | None],
    config: Config,
) -> str:
    """Return the status the evidence supports.

    A run with too much history for ``insufficient data`` can still hold too
    little held-out evidence to compare anything: every comparison may have
    been paired over too few rows, or over rows that form a single block, in
    which case every replication averages the same values. That is reported
    separately rather than as a negative result.

    Returns
    -------
    str
        ``ok``, ``insufficient data`` or ``insufficient held-out evidence``.
    """
    if labelled < config.minimum_labelled_events:
        return STATUS_INSUFFICIENT
    if not any(_is_evidence(interval, config) for interval in against_base.values()):
        return STATUS_INSUFFICIENT_HELDOUT
    return STATUS_OK


def _is_evidence(interval: Interval | None, config: Config) -> bool:
    """Return whether an interval carries enough held-out evidence to judge.

    Returns
    -------
    bool
        Whether the interval was paired over enough rows, in at least two
        blocks, for its width to mean anything.
    """
    return (
        interval is not None
        and interval.rows >= config.minimum_scored_events
        and interval.blocks >= MINIMUM_BLOCKS
    )


def _verdict(
    status: str,
    labelled: int,
    against_base: cabc.Mapping[str, Interval | None],
    config: Config,
) -> str:
    """State the outcome in one sentence, negative results included.

    A predictor may only be said to beat the base rate when its interval lies
    wholly below zero *and* it was paired over enough held-out rows, spread
    over at least two blocks, for that interval to mean anything.

    Returns
    -------
    str
        The verdict.
    """
    if status == STATUS_INSUFFICIENT:
        return (
            f"insufficient data: {labelled} labelled events, "
            f"{config.minimum_labelled_events} required"
        )
    if status == STATUS_INSUFFICIENT_HELDOUT:
        return (
            "insufficient held-out evidence: no predictor was paired over "
            f"{config.minimum_scored_events} rows in two or more blocks of "
            f"{config.block_size}"
        )
    improving = [
        name
        for name, interval in against_base.items()
        if interval is not None and _is_evidence(interval, config) and interval.high < 0
    ]
    if not improving:
        return "nothing beats the base rate on the held-out period"
    return "beats the base rate on the held-out period: " + ", ".join(sorted(improving))
