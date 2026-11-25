"""Run the ensembles, score every predictor and choose the held-out verdict.

Every prediction is made at its event's admission moment, so the tuning rows a
weight fit may read are bounded by the first held-out admission rather than by
the last training label. The fixed ensemble's weights are fitted once on those
rows and then applied to the held-out period. Its training rows are excluded
from its score: the weights were fitted on their labels, so scoring them would
report a retrospective fit as a prediction. Only the comparison the weights
could not see is reported, and the document states that limitation.

A source that records no admission time supports no prediction at all. The
evaluation then reports insufficient admission times rather than treating a
missing timestamp as evidence that the label arrived early. A source that
records admission times but never records when a label became available is
reported separately: the decision time is trusted, but the label's own
availability is not, and more labels alone do not prove chronological
validity.
"""

import dataclasses as dc
import typing as typ

from buildlimits.predict.comparison import compare
from buildlimits.predict.ensemble_runs import EnsembleRun, run_ablations, run_ensembles
from buildlimits.predict.scoring import ablation_metrics, all_metrics

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.bootstrap import Interval
    from buildlimits.predict.config import Config
    from buildlimits.predict.harness import Walk
    from buildlimits.predict.metrics import Metrics

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient data"
STATUS_INSUFFICIENT_ADMISSION = "insufficient admission times"
STATUS_INSUFFICIENT_AVAILABILITY = "insufficient label-availability times"
STATUS_INSUFFICIENT_HELDOUT = "insufficient held-out evidence"
# One block resamples to itself, so its interval has no width to judge.
MINIMUM_BLOCKS = 2


@dc.dataclass(frozen=True, slots=True)
class _EvidenceCounts:
    """The population counts every status and verdict sentence is decided from.

    They travel together because each gate is read against the others: a label
    shortage, a decision shortage and an availability shortage are told apart
    by comparing these three numbers, so passing them separately invites a
    caller to pair one gate's denominator with another gate's numerator.
    """

    labelled: int
    usable_labelled: int
    predictable: int
    unavailable_availability: int


@dc.dataclass(frozen=True, slots=True)
class Evaluation:
    """Every comparison the benchmark reports.

    Attributes
    ----------
    status : str
        ``ok`` or one of the insufficient-evidence statuses.
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
    verdict_alpha : float
        The tail probability each comparison with the base rate is tested at,
        after the configured level has been shared across the whole family of
        comparisons that could be named.
    best_single : str | None
        The single predictor with the best Brier on the tuning rows the
        weight fit may read.
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
    verdict_alpha: float
    best_single: str | None
    against_best_single: dict[str, Interval | None]
    verdict: str
    minimum_scored_events: int

    def ranking(self) -> tuple[str, ...]:
        """Order every predictor by held-out Brier.

        Every predictor with at least one scored held-out row is ranked by its
        Brier score, so the table never hides a poor result. A predictor that
        scored fewer than ``minimum_scored_events`` rows is placed after every
        qualified predictor rather than in the position its score would earn:
        a mean over too few rows is still reported, but it must not displace a
        predictor whose score rests on enough evidence. Names without any
        held-out score follow the ranked ones.

        Returns
        -------
        tuple[str, ...]
            Names with a held-out score, best first; scoreless names follow.
        """
        ordered = [
            name
            for name, value in sorted(self._heldout_brier().items(), key=_brier_order)
            if value is not None
        ]
        qualified = [name for name in ordered if self.meets_minimum(name)]
        underpowered = [name for name in ordered if name not in qualified]
        return (
            tuple(qualified)
            + tuple(underpowered)
            + tuple(name for name in self.metrics if name not in ordered)
        )

    def meets_minimum(self, name: str) -> bool:
        """Return whether a predictor scored enough held-out rows to rank.

        Returns
        -------
        bool
            Whether its held-out score covers ``minimum_scored_events`` rows.
        """
        return self.metrics[name]["held-out"].scored >= self.minimum_scored_events

    def _heldout_brier(self) -> dict[str, float | None]:
        """Return each predictor's held-out Brier score.

        Returns
        -------
        dict[str, float | None]
            Brier per predictor, None where it answered nothing.
        """
        return {name: values["held-out"].brier for name, values in self.metrics.items()}


def _brier_order(item: tuple[str, float | None]) -> tuple[int, float]:
    """Sort a predictor's Brier score, placing a scoreless one last.

    Returns
    -------
    tuple[int, float]
        A rank flag and the score, so None never compares against a float.
    """
    _, value = item
    return (1, 0.0) if value is None else (0, value)


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
    compared = compare(walk, ensembles, config)
    counts = _EvidenceCounts(
        labelled=walk.labelled,
        usable_labelled=walk.usable_labelled,
        predictable=_predictable_count(walk),
        unavailable_availability=walk.unavailable_availability,
    )
    status = _status(counts, compared.against_base_rate, config)
    return Evaluation(
        status=status,
        walk=walk,
        ensembles=ensembles,
        ablations=ablations,
        metrics=metrics,
        ablation_metrics=ablated,
        against_base_rate=compared.against_base_rate,
        verdict_alpha=compared.verdict_alpha,
        best_single=compared.best_single,
        against_best_single=compared.against_best_single,
        verdict=_verdict(
            counts,
            compared.against_base_rate,
            config,
            withheld=walk.unverified_label_time,
        ),
        minimum_scored_events=config.minimum_scored_events,
    )


def _predictable_count(walk: Walk) -> int:
    """Count the events that carry both a phase and a feature row.

    An event is only predictable when the replay both placed it in a period and
    measured it at its admission, so the two columns are read together.

    Returns
    -------
    int
        The number of predictable events.
    """
    return sum(
        1
        for phase, row in zip(walk.phases, walk.rows, strict=True)
        if phase is not None and row is not None
    )


def _status(
    counts: _EvidenceCounts,
    against_base: cabc.Mapping[str, Interval | None],
    config: Config,
) -> str:
    """Return the status the evidence supports.

    The minimum-label gate counts independent labelled rows, so repeated
    appends cannot clear it. A run that clears it can still hold no event with a
    usable admission time, and then nothing could be predicted at all. A source
    that records the decision but never the moment a label became available is
    reported separately, because the decision time alone cannot prove the label
    was knowable before a later prediction. When predictions do exist, too few
    paired rows, or rows forming a single block, are likewise reported
    separately.

    Returns
    -------
    str
        ``ok``, ``insufficient data``, ``insufficient admission times``,
        ``insufficient label-availability times`` or ``insufficient held-out
        evidence``.
    """
    if counts.usable_labelled < config.minimum_labelled_events:
        return STATUS_INSUFFICIENT
    if counts.predictable == 0:
        if counts.unavailable_availability:
            return STATUS_INSUFFICIENT_AVAILABILITY
        return STATUS_INSUFFICIENT_ADMISSION
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
    counts: _EvidenceCounts,
    against_base: cabc.Mapping[str, Interval | None],
    config: Config,
    *,
    withheld: int = 0,
) -> str:
    """State the outcome in one sentence, negative results included.

    A predictor may only be said to beat the base rate when its interval lies
    wholly below zero *and* it was paired over enough held-out rows, spread
    over at least two blocks, for that interval to mean anything; the interval
    is already widened for the family of comparisons.

    A history whose labels were withheld because their recorded time is
    unverified must not read as one that simply carried few labels: the
    insufficient-data sentence names the withheld count beside the verified
    one.

    Returns
    -------
    str
        The verdict.
    """
    status = _status(counts, against_base, config)
    if status != STATUS_OK:
        return _insufficient_verdict(status, counts, config, withheld=withheld)
    improving = _improving_predictors(against_base, config)
    if not improving:
        return "nothing beats the base rate on the held-out period"
    return "beats the base rate on the held-out period: " + ", ".join(sorted(improving))


def _insufficient_verdict(
    status: str,
    counts: _EvidenceCounts,
    config: Config,
    *,
    withheld: int,
) -> str:
    """Name the gate an insufficient run met, in its own sentence.

    Returns
    -------
    str
        The sentence for the supplied insufficient-evidence status.
    """
    if status == STATUS_INSUFFICIENT:
        return (
            f"insufficient data: {counts.usable_labelled} independent labelled "
            f"events, {config.minimum_labelled_events} required"
            + _withheld_note(withheld)
        )
    if status == STATUS_INSUFFICIENT_ADMISSION:
        return (
            f"insufficient admission times: {counts.labelled} labelled events, "
            f"{counts.predictable} with an admission time at or before their "
            "own label"
        )
    if status == STATUS_INSUFFICIENT_AVAILABILITY:
        return (
            f"insufficient label-availability times: {counts.labelled} labelled "
            f"events, {counts.unavailable_availability} with an admission time "
            "but no verified label-availability time"
        )
    return (
        "insufficient held-out evidence: no predictor was paired over "
        f"{config.minimum_scored_events} rows in two or more blocks of "
        f"{config.block_size}"
    )


def _improving_predictors(
    against_base: cabc.Mapping[str, Interval | None], config: Config
) -> list[str]:
    """Return the predictors whose interval lies wholly below zero.

    Returns
    -------
    list[str]
        The predictor names, unsorted.
    """
    return [
        name
        for name, interval in against_base.items()
        if interval is not None and _is_evidence(interval, config) and interval.high < 0
    ]


def _withheld_note(withheld: int) -> str:
    """Name the labels a run withheld because their time is unverified.

    A store row's recorded label time can be a lower bound rather than proof,
    so those labels are neither released nor scored. Reporting the count keeps
    a withheld history distinct from one that recorded few labels at all.

    Returns
    -------
    str
        A clause naming the count, or the empty string when none was withheld.

    Examples
    --------
    >>> _withheld_note(0)
    ''
    >>> _withheld_note(3)
    '; 3 more withheld because their recorded label time is unverified'
    """
    return (
        ""
        if not withheld
        else f"; {withheld} more withheld because their recorded label time is "
        "unverified"
    )
