"""Pair every predictor with the base rate and with the best single.

A comparison is held-out only and paired over contiguous stream positions, so
an abstention cannot splice distant rows into one bootstrap block. Each one is
tested at a level shared across the family of predictors that could be named,
which keeps the verdict as a whole at the configured level.
"""

import dataclasses as dc
import typing as typ

from buildlimits.predict.bootstrap import Interval, block_bootstrap, paired_differences
from buildlimits.predict.ensemble_runs import EnsembleRun, tuning_indices
from buildlimits.predict.scoring import heldout_rows, rows_for, series_map, summarize

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.config import Config
    from buildlimits.predict.harness import Walk


def compare(
    walk: Walk,
    ensembles: cabc.Mapping[str, EnsembleRun],
    config: Config,
) -> Comparisons:
    """Compare every predictor with the base rate and the best single.

    Every comparison with the base rate is tested at a level shared across the
    whole family of predictors that could be named. Testing each at the full
    configured level would give the verdict several independent chances to
    clear zero on noise, which is not what that level claims.

    Returns
    -------
    Comparisons
        The held-out intervals and the tuning-period winner.
    """
    series = series_map(walk, ensembles)
    comparisons = _Comparison(walk, series)
    best_single = best_on_training(walk, series, walk.components(), config)
    family = [name for name in series if name != "base-rate"]
    alpha = verdict_alpha(config, len(family))
    corrected = dc.replace(config, bootstrap_alpha=alpha)
    return Comparisons(
        against_base_rate={
            name: comparisons.versus(name, "base-rate", corrected) for name in family
        },
        verdict_alpha=alpha,
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


def verdict_alpha(config: Config, family: int) -> float:
    """Return the level one comparison with the base rate is tested at.

    Bonferroni divides the configured level across the family, so the verdict
    as a whole holds at that level even though any one predictor could be
    named. The cost is a wider interval for each predictor, which is the
    honest price of asking several of them at once.

    Returns
    -------
    float
        The two-sided tail probability applied to each comparison.
    """
    return config.bootstrap_alpha / max(1, family)


@dc.dataclass(frozen=True, slots=True)
class Comparisons:
    """The held-out comparisons assembled for one evaluation.

    Attributes
    ----------
    against_base_rate : dict[str, Interval | None]
        Each predictor's Brier difference from the base rate.
    verdict_alpha : float
        The level those differences are tested at, shared across the family.
    best_single : str | None
        The tuning-period winner among the singles.
    against_best_single : dict[str, Interval | None]
        Each ensemble's difference from that winner.
    """

    against_base_rate: dict[str, Interval | None]
    verdict_alpha: float
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


def best_on_training(
    walk: Walk,
    series: cabc.Mapping[str, cabc.Sequence[float | None]],
    singles: cabc.Sequence[str],
    config: Config,
) -> str | None:
    """Pick the single predictor with the lowest tuning-row Brier.

    The rows are the ones the fixed ensemble's weight fit may read: the last
    tuning batch is withheld from both, so the predictor every ensemble is
    compared against was chosen on the same history those weights may use.
    Choosing it from every tuning row would let a batch no ensemble could
    learn from decide the comparator.

    The candidates are also scored on one population: only the tuning events
    every contender answered. A predictor that abstains on the hard rows
    otherwise competes on a smaller, easier set and can win on rows its rivals
    never saw, which would then become the baseline every ensemble is measured
    against. A single that answered too little to be ranked cannot become that
    baseline either, so it does not shrink the population the others share.

    Returns
    -------
    str | None
        The winner, or None when no single predictor scored enough rows.
    """
    tuning = frozenset(tuning_indices(walk))
    contenders = [
        name
        for name in singles
        if sum(1 for index in tuning if series[name][index] is not None)
        >= config.minimum_scored_events
    ]
    common = frozenset(
        index
        for index in tuning
        if all(series[name][index] is not None for name in contenders)
    )
    best: tuple[str, float] | None = None
    for name in contenders:
        block = summarize(
            rows_for(walk, name, series[name], indices=common),
            clip=config.log_loss_clip,
            threshold=config.decision_threshold,
            bins=config.calibration_bins,
        )
        if block.scored < config.minimum_scored_events or block.brier is None:
            continue
        if best is None or block.brier < best[1]:
            best = (name, block.brier)
    return None if best is None else best[0]
