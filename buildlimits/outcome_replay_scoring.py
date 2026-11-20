"""Accumulate H2 and H3 evidence one row at a time.

Both hypotheses judge a history that may be far larger than memory, so their
evidence is gathered by folds rather than by collecting rows. The H3 fold
judges each row as it passes; the H2 fold surveys the stream and then scores
the same rows in store order. The H2 fold keeps one bounded runtime window per
key that can be scored, so neither hypothesis grows with the number of rows.
The report types and their verdict thresholds live in ``outcome_replay``; this
module owns the counting behind them.
"""

import collections
import dataclasses as dc
import statistics
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_key import CacheKey
from buildlimits.outcome_evidence import Coverage, CoverageBuilder
from buildlimits.outcome_record_format import (
    OutcomeRecord,
    StoreRecord,
    WindowSummary,
)

# A key is paired for H2 when it was seen under both of these labels.
H2_LABEL_COUNT = 2
# The runtime clue compares a row with the median of the key's recent hits. A
# key can be built thousands of times, so keeping every hit it ever had would
# let one hot key grow the fold with the history. Sixty-four hits give a stable
# median while bounding each scored key's share of memory.
H2_RUNTIME_WINDOW = 64


@dc.dataclass(slots=True)
class H3Tally:
    """Accumulate H3 evidence one row at a time.

    A streaming reader yields each row once, so the tally never holds the
    rows it has judged.
    """

    attributed: int = 0
    evaluated: int = 0
    counterexamples: int = 0
    summaries: int = 0
    span: CoverageBuilder = dc.field(default_factory=CoverageBuilder)

    def observe(self, record: StoreRecord) -> None:
        """Judge one row and widen the covered span to include it.

        Parameters
        ----------
        record : StoreRecord
            One row in store order.
        """
        match record:
            case WindowSummary():
                self.summaries += 1
                self.counterexamples += not summary_agrees(record)
            case OutcomeRecord():
                self.attributed += record.outcome != "unattributed"
                agrees = row_agrees(record)
                self.evaluated += agrees is not None
                self.counterexamples += agrees is False
            case _:
                return
        self.span.observe(record.observed_at)


def row_agrees(record: OutcomeRecord) -> bool | None:
    """Check one attributed row against its parser class.

    ``declined`` and ``not_cacheable`` are separate refusal classes under ADR
    005, and an ``unsupported`` row reports no parser result to evaluate.

    Returns
    -------
    bool | None
        Whether the row agrees with its parser class, or None when unevaluable.
    """
    if record.outcome in {"unattributed", "unsupported"}:
        return None
    refusals = {"declined", "not_compile"}
    if record.parser_class == "cacheable":
        return record.outcome not in refusals
    if record.parser_class in refusals:
        return record.outcome == record.parser_class
    return None


def summary_agrees(summary: WindowSummary) -> bool:
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


def reiterable(records: cabc.Iterable[StoreRecord]) -> cabc.Iterable[StoreRecord]:
    """Return an input H2 can survey and then score.

    A one-shot iterator would be exhausted by the survey, so it is collected
    once; a re-iterable history stream is passed through untouched.

    Returns
    -------
    cabc.Iterable[StoreRecord]
        A value whose two passes see the same rows.
    """
    return tuple(records) if iter(records) is records else records


@dc.dataclass(frozen=True, slots=True)
class H2Survey:
    """What H2's first pass learns about the whole input."""

    attributed: int
    paired_keys: frozenset[CacheKey]
    coverage: Coverage


def survey_h2(records: cabc.Iterable[StoreRecord]) -> H2Survey:
    """Count attributed rows and find keys seen as both hit and miss.

    Returns
    -------
    H2Survey
        The counts, paired keys and covered span of one full pass.
    """
    labels: dict[CacheKey, set[str]] = collections.defaultdict(set)
    attributed = 0
    span = CoverageBuilder()
    for record in records:
        match record:
            case WindowSummary():
                span.observe(record.observed_at)
            case OutcomeRecord():
                span.observe(record.observed_at)
                attributed += record.outcome != "unattributed"
                if record.outcome in {"hit", "miss"}:
                    labels[record.key].add(record.outcome)
    paired = frozenset(
        key for key, seen in labels.items() if len(seen) == H2_LABEL_COUNT
    )
    return H2Survey(attributed, paired, span.coverage())


def score_h2(
    records: cabc.Iterable[StoreRecord], paired_keys: frozenset[CacheKey]
) -> collections.Counter[str]:
    """Score each evaluable row in store order.

    Only a paired key can produce an evaluable row, so only those keys keep a
    runtime window; a key seen under a single label costs no memory per row.

    Returns
    -------
    collections.Counter[str]
        The counters the H2 report is built from.
    """
    earlier_hits: dict[CacheKey, collections.deque[float]] = {}
    counters: collections.Counter[str] = collections.Counter()
    for record in records:
        if isinstance(record, OutcomeRecord):
            _score_h2_record(record, paired_keys, earlier_hits, counters)
    return counters


def _score_h2_record(
    record: OutcomeRecord,
    paired_keys: frozenset[CacheKey],
    earlier_hits: dict[CacheKey, collections.deque[float]],
    counters: collections.Counter[str],
) -> None:
    """Accumulate one row while preserving store-order runtime history."""
    if record.outcome not in {"hit", "miss"} or record.key not in paired_keys:
        return
    previous_hits = earlier_hits.get(record.key)
    runtime_miss = previous_hits is not None and record.runtime_seconds > max(
        1.0, 3 * statistics.median(previous_hits)
    )
    if record.child_samples >= 1:
        _score_h2_sighting(
            record,
            runtime_miss=runtime_miss,
            counters=counters,
        )
    if record.outcome == "hit":
        if previous_hits is None:
            previous_hits = earlier_hits[record.key] = collections.deque(
                maxlen=H2_RUNTIME_WINDOW
            )
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
    prediction = "miss" if combined_miss else "hit"
    counters["combined_correct"] += prediction == record.outcome
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
