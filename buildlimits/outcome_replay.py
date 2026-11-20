"""Read-only H2 and H3 checks over bounded cache-outcome store values.

The report types and their fixed verdict thresholds live here; the folds that
count the evidence as it streams past live in
``buildlimits.outcome_replay_scoring``.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_evidence import (
    H2_LIMITS,
    H3_LIMITS,
    Coverage,
)
from buildlimits.outcome_replay_scoring import (
    H3Tally,
    reiterable,
    score_h2,
    survey_h2,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_record_format import StoreRecord

type Verdict = typ.Literal[
    "supported", "refuted", "inconclusive", "insufficient", "not_collected"
]
type H2Verdict = typ.Literal["supported", "refuted", "inconclusive", "insufficient"]

MIN_ATTRIBUTED_OUTCOMES = 2_000
H2_MIN_EVALUABLE_RECORDS = 100
H2_SUPPORTED_AGREEMENT_PERCENT = 95
H2_REFUTED_BELOW_PERCENT = 90
REPLAY_SCHEMA = 1


@dc.dataclass(frozen=True, slots=True)
class H3Report:
    """Bounded evidence for the rustc-parser prediction hypothesis."""

    verdict: Verdict
    attributed_outcomes: int
    evaluated_windows: int
    evaluated_rows: int
    counterexamples: int
    coverage: Coverage

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
            "covered": self.coverage.as_dict(),
            "caveats": list(H3_LIMITS),
        }


def evaluate_h3(records: cabc.Iterable[StoreRecord]) -> H3Report:
    """Compare parser predictions with attributed rows and refusal counters.

    One counterexample refutes H3. A positive result requires the fixed 2,000
    attributed-outcome minimum and at least one balanced window summary. The
    input is read once, so a streaming history keeps memory bounded.

    Returns
    -------
    H3Report
        The threshold verdict and its bounded agreement counts.
    """
    tally = H3Tally()
    for record in records:
        tally.observe(record)
    return H3Report(
        verdict=_h3_verdict(
            tally.counterexamples,
            tally.attributed,
            has_summaries=bool(tally.summaries),
        ),
        attributed_outcomes=tally.attributed,
        evaluated_windows=tally.summaries,
        evaluated_rows=tally.evaluated,
        counterexamples=tally.counterexamples,
        coverage=tally.span.coverage(),
    )


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
    coverage: Coverage

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
            "covered": self.coverage.as_dict(),
            "caveats": list(H2_LIMITS),
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
    repeated = reiterable(records)
    survey = survey_h2(repeated)
    counters = score_h2(repeated, survey.paired_keys)
    evaluable = counters["evaluable"]
    correct = counters["combined_correct"]
    return H2Report(
        verdict=_h2_verdict(survey.attributed, evaluable, correct),
        attributed_outcomes=survey.attributed,
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
        coverage=survey.coverage,
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


def _agreement(correct: int, total: int) -> float | None:
    """Return a rounded fraction, omitting rates with an empty denominator.

    Returns
    -------
    float | None
        Rounded agreement fraction, or None when there are no observations.
    """
    return round(correct / total, 6) if total else None
