"""Read-only H2 and H3 checks over bounded cache-outcome store values."""

import collections
import dataclasses as dc
import statistics
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_key import CacheKey
from buildlimits.outcome_record_format import OutcomeRecord, StoreRecord, WindowSummary

type Verdict = typ.Literal[
    "supported", "refuted", "inconclusive", "insufficient", "not_collected"
]
type H2Verdict = typ.Literal["supported", "refuted", "inconclusive", "insufficient"]

MIN_ATTRIBUTED_OUTCOMES = 2_000
H2_MIN_EVALUABLE_RECORDS = 100
H2_SUPPORTED_AGREEMENT_PERCENT = 95
H2_REFUTED_BELOW_PERCENT = 90
REPLAY_SCHEMA = 1
H2_LABEL_COUNT = 2


@dc.dataclass(frozen=True, slots=True)
class H3Report:
    """Bounded evidence for the rustc-parser prediction hypothesis."""

    verdict: Verdict
    attributed_outcomes: int
    evaluated_windows: int
    evaluated_rows: int
    counterexamples: int

    def as_dict(self) -> dict[str, object]:
        """Return the stable command-line JSON shape.

        Returns
        -------
        dict[str, object]
            The versioned, privacy-safe report fields.
        """
        return {
            "schema": REPLAY_SCHEMA,
            "hypothesis": "h3",
            "verdict": self.verdict,
            "attributed_outcomes": self.attributed_outcomes,
            "evaluated_windows": self.evaluated_windows,
            "evaluated_rows": self.evaluated_rows,
            "counterexamples": self.counterexamples,
        }


def evaluate_h3(records: cabc.Iterable[StoreRecord]) -> H3Report:
    """Compare parser predictions with attributed rows and refusal counters.

    One counterexample refutes H3. A positive result requires the fixed 2,000
    attributed-outcome minimum and at least one balanced window summary.

    Returns
    -------
    H3Report
        The threshold verdict and its bounded agreement counts.
    """
    outcomes, summaries = _split_records(records)
    attributed = sum(record.outcome != "unattributed" for record in outcomes)
    row_checks = tuple(_row_counterexample(record) for record in outcomes)
    counterexamples = sum(check is False for check in row_checks)
    counterexamples += sum(not _summary_agrees(summary) for summary in summaries)
    return H3Report(
        verdict=_h3_verdict(counterexamples, attributed, has_summaries=bool(summaries)),
        attributed_outcomes=attributed,
        evaluated_windows=len(summaries),
        evaluated_rows=sum(check is not None for check in row_checks),
        counterexamples=counterexamples,
    )


def _split_records(
    records: cabc.Iterable[StoreRecord],
) -> tuple[tuple[OutcomeRecord, ...], tuple[WindowSummary, ...]]:
    """Separate outcome rows from window summaries in store order.

    Returns
    -------
    tuple[tuple[OutcomeRecord, ...], tuple[WindowSummary, ...]]
        Outcome rows, then window summaries.
    """
    values = tuple(records)
    outcomes = tuple(record for record in values if isinstance(record, OutcomeRecord))
    summaries = tuple(record for record in values if isinstance(record, WindowSummary))
    return outcomes, summaries


def _h3_verdict(
    counterexamples: int, attributed: int, *, has_summaries: bool
) -> Verdict:
    """Reduce H3 evidence to its fixed verdict order.

    Returns
    -------
    Verdict
        ``refuted`` on any counterexample, otherwise the threshold verdict.
    """
    if counterexamples:
        return "refuted"
    if attributed < MIN_ATTRIBUTED_OUTCOMES:
        return "insufficient"
    if not has_summaries:
        return "not_collected"
    return "supported"


@dc.dataclass(frozen=True, slots=True)
class H2Report:
    """Bounded agreement evidence for child and runtime prediction."""

    verdict: H2Verdict
    attributed_outcomes: int
    evaluable_records: int
    combined_correct: int
    runtime_correct: int
    child_evaluable_records: int
    child_correct: int
    seen_records: int
    not_seen_records: int
    ambiguous_records: int
    unavailable_records: int
    misses_seen: int
    misses_with_child_observation: int

    @property
    def combined_agreement(self) -> float | None:
        """Fraction of combined correct predictions, when available.

        Returns
        -------
        float | None
            Correct predictions divided by evaluable rows, or None when empty.
        """
        return _agreement(self.combined_correct, self.evaluable_records)

    @property
    def runtime_only_agreement(self) -> float | None:
        """Fraction of runtime-only correct predictions, when available.

        Returns
        -------
        float | None
            Runtime-only correct predictions divided by evaluable rows.
        """
        return _agreement(self.runtime_correct, self.evaluable_records)

    @property
    def child_only_agreement(self) -> float | None:
        """Fraction of child-only correct predictions, when available.

        Returns
        -------
        float | None
            Child-only correct predictions divided by observed child rows.
        """
        return _agreement(self.child_correct, self.child_evaluable_records)

    @property
    def miss_sighting_share(self) -> float | None:
        """Share of observed misses with a unique child sighting.

        Returns
        -------
        float | None
            Seen labelled misses divided by misses with child observations.
        """
        return _agreement(self.misses_seen, self.misses_with_child_observation)

    def as_dict(self) -> dict[str, object]:
        """Return stable JSON fields without exposing keys or source values.

        Returns
        -------
        dict[str, object]
            The versioned H2 report with bounded counts and agreement rates.
        """
        return {
            "schema": REPLAY_SCHEMA,
            "hypothesis": "h2",
            "verdict": self.verdict,
            "attributed_outcomes": self.attributed_outcomes,
            "evaluable_records": self.evaluable_records,
            "combined": {
                "correct": self.combined_correct,
                "agreement": self.combined_agreement,
            },
            "runtime_only": {
                "correct": self.runtime_correct,
                "agreement": self.runtime_only_agreement,
            },
            "child_only": {
                "correct": self.child_correct,
                "evaluable_records": self.child_evaluable_records,
                "agreement": self.child_only_agreement,
            },
            "child_sightings": {
                "seen": self.seen_records,
                "not_seen": self.not_seen_records,
                "ambiguous": self.ambiguous_records,
                "unavailable": self.unavailable_records,
            },
            "miss_sightings": {
                "seen": self.misses_seen,
                "observed": self.misses_with_child_observation,
                "share": self.miss_sighting_share,
            },
        }


def evaluate_h2(records: cabc.Iterable[StoreRecord]) -> H2Report:
    """Evaluate the fixed child-plus-runtime classifier in store order.

    Only hit and miss rows with at least one sample and a key containing both
    labels are evaluable. The runtime baseline uses all earlier hits for that
    key, whether or not those earlier rows were sampled.

    Returns
    -------
    H2Report
        Agreement, sighting counts and the threshold verdict.
    """
    values = tuple(record for record in records if isinstance(record, OutcomeRecord))
    attributed = sum(record.outcome != "unattributed" for record in values)
    paired_keys = _paired_keys(values)
    earlier_hits: dict[CacheKey, list[float]] = collections.defaultdict(list)
    counters: collections.Counter[str] = collections.Counter()
    for record in values:
        _score_h2_record(record, paired_keys, earlier_hits, counters)

    evaluable = counters["evaluable"]
    correct = counters["combined_correct"]
    return H2Report(
        verdict=_h2_verdict(attributed, evaluable, correct),
        attributed_outcomes=attributed,
        evaluable_records=evaluable,
        combined_correct=correct,
        runtime_correct=counters["runtime_correct"],
        child_evaluable_records=counters["child_evaluable"],
        child_correct=counters["child_correct"],
        seen_records=counters["seen"],
        not_seen_records=counters["not_seen"],
        ambiguous_records=counters["ambiguous"],
        unavailable_records=counters["unavailable"],
        misses_seen=counters["misses_seen"],
        misses_with_child_observation=counters["misses_with_child_observation"],
    )


def _h2_verdict(attributed: int, evaluable: int, correct: int) -> H2Verdict:
    """Reduce H2 agreement to its fixed threshold verdict.

    Returns
    -------
    H2Verdict
        The agreement verdict, or ``insufficient`` below either minimum.
    """
    if attributed < MIN_ATTRIBUTED_OUTCOMES or evaluable < H2_MIN_EVALUABLE_RECORDS:
        return "insufficient"
    if correct * 100 >= H2_SUPPORTED_AGREEMENT_PERCENT * evaluable:
        return "supported"
    if correct * 100 < H2_REFUTED_BELOW_PERCENT * evaluable:
        return "refuted"
    return "inconclusive"


def _paired_keys(records: tuple[OutcomeRecord, ...]) -> frozenset[CacheKey]:
    """Find keys whose full store history includes both hit and miss labels.

    Returns
    -------
    frozenset[CacheKey]
        Keys with at least one hit and one miss in the full input.
    """
    outcomes: dict[CacheKey, set[str]] = collections.defaultdict(set)
    for record in records:
        if record.outcome in {"hit", "miss"}:
            outcomes[record.key].add(record.outcome)
    return frozenset(
        key for key, labels in outcomes.items() if len(labels) == H2_LABEL_COUNT
    )


def _score_h2_record(
    record: OutcomeRecord,
    paired_keys: frozenset[CacheKey],
    earlier_hits: dict[CacheKey, list[float]],
    counters: collections.Counter[str],
) -> None:
    """Accumulate one row while preserving store-order runtime history."""
    if record.outcome not in {"hit", "miss"}:
        return
    previous_hits = earlier_hits[record.key]
    runtime_miss = bool(previous_hits) and record.runtime_seconds > max(
        1.0, 3 * statistics.median(previous_hits)
    )
    if record.child_samples >= 1 and record.key in paired_keys:
        _score_h2_sighting(
            record,
            runtime_miss=runtime_miss,
            counters=counters,
        )
    if record.outcome == "hit":
        previous_hits.append(record.runtime_seconds)


def _score_h2_sighting(
    record: OutcomeRecord,
    *,
    runtime_miss: bool,
    counters: collections.Counter[str],
) -> None:
    """Count one sampled paired-key row and its applicable predictors."""
    sighting = record.child_sighting
    combined_miss = sighting == "seen" or runtime_miss
    counters["evaluable"] += 1
    counters[sighting] += 1
    counters["combined_correct"] += (
        "miss" if combined_miss else "hit"
    ) == record.outcome
    counters["runtime_correct"] += ("miss" if runtime_miss else "hit") == record.outcome
    if sighting in {"seen", "not_seen"}:
        _score_child_sighting(record, sighting, counters)


def _score_child_sighting(
    record: OutcomeRecord,
    sighting: str,
    counters: collections.Counter[str],
) -> None:
    """Count a directly usable child sighting and its miss coverage."""
    child_miss = sighting == "seen"
    counters["child_evaluable"] += 1
    counters["child_correct"] += ("miss" if child_miss else "hit") == record.outcome
    if record.outcome == "miss":
        counters["misses_with_child_observation"] += 1
        counters["misses_seen"] += child_miss


def _agreement(correct: int, total: int) -> float | None:
    """Return a rounded fraction, omitting rates with an empty denominator.

    Returns
    -------
    float | None
        Rounded agreement fraction, or None when there are no observations.
    """
    return round(correct / total, 6) if total else None


def _row_counterexample(record: OutcomeRecord) -> bool | None:
    """Check only parser classes with a directly attributable refusal label.

    Returns
    -------
    bool | None
        Whether the row contradicts its parser class, or None when it cannot
        be directly evaluated.
    """
    if record.outcome == "unattributed":
        return None
    if record.parser_class == "not_compile":
        return record.outcome == "not_compile"
    if record.parser_class == "declined":
        return record.outcome in {"declined", "not_cacheable"}
    return None


def _summary_agrees(summary: WindowSummary) -> bool:
    """Compare refusal classes independently to their sccache counters.

    Returns
    -------
    bool
        Whether both refusal counters match their predicted counts.
    """
    return (
        summary.predicted_not_compile == summary.requests_not_compile
        and summary.predicted_declined == summary.requests_not_cacheable
    )
