"""H2 replay classification, fixed thresholds and CLI reports."""

import dataclasses as dc
import datetime as dt
import json
import typing as typ
from pathlib import Path

import pytest
from outcome_test_helpers import OBSERVED_AT, cache_key

from buildlimits import (
    outcome_evidence,
    outcome_replay,
    outcome_replay_cli,
    outcome_store_io,
)
from buildlimits.outcome_record_format import ChildSighting, OutcomeRecord, StoreHeader
from buildlimits.outcome_sampling import CHILD_SAMPLE_SECONDS

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

    from buildlimits.outcome_key import CacheKey
    from buildlimits.outcome_vocabulary import CacheResult


_ADR = Path(__file__).parents[1] / "docs/adr-005-cache-outcome-recording.md"


def _key(index: int) -> CacheKey:
    """Create a stable, path-free key for one synthetic history.

    Returns
    -------
    CacheKey
        One key with a unique, non-path argument digest.
    """
    return dc.replace(cache_key(), argument_digest=f"{index:064x}")


def _outcome(
    key: CacheKey,
    label: CacheResult,
    *,
    runtime: float,
    sighting: ChildSighting = "not_seen",
) -> OutcomeRecord:
    """Build one store row with explicit H2 inputs.

    Returns
    -------
    OutcomeRecord
        One deterministic cache history row.
    """
    return OutcomeRecord(
        key=key,
        outcome=label,
        origin="registry",
        runtime_seconds=runtime,
        observed_at=OBSERVED_AT,
        parser_class="cacheable",
        child_sighting=sighting,
        child_samples=0 if label == "failed" else 1,
    )


def _rows(
    evaluable: int,
    correct: int,
    attributed: int,
    *,
    status_overrides: dict[int, ChildSighting] | None = None,
) -> tuple[OutcomeRecord, ...]:
    """Build paired key histories with a chosen number of combined matches.

    Returns
    -------
    tuple[OutcomeRecord, ...]
        Sampled paired rows plus unattributed-to-H2 supporting rows.
    """
    overrides = {} if status_overrides is None else status_overrides
    records = [
        _paired_row(index, evaluable, correct, overrides) for index in range(evaluable)
    ]
    records.extend(
        _outcome(_key(100_000 + index), "failed", runtime=0.25)
        for index in range(len(records), attributed)
    )
    return tuple(records)


def _paired_row(
    index: int,
    evaluable: int,
    correct: int,
    overrides: dict[int, ChildSighting],
) -> OutcomeRecord:
    """Build one sampled paired row, honouring an explicit sighting override.

    Returns
    -------
    OutcomeRecord
        The paired hit or miss row for ``index``.
    """
    key_index, label = _row_identity(index, evaluable)
    is_correct = index < correct or index in overrides
    sighting = overrides[index] if index in overrides else _sighting(label, is_correct)
    runtime = _row_runtime(label, is_correct)
    if index in overrides and label == "miss":
        runtime = 2.0
    return _outcome(_key(key_index), label, runtime=runtime, sighting=sighting)


def _row_identity(index: int, evaluable: int) -> tuple[int, CacheResult]:
    """Choose the paired key and label for one evaluable row index.

    Rows in the paired region alternate hit and miss on shared keys; any
    remainder row is a single extra hit on the first key.

    Returns
    -------
    tuple[int, CacheResult]
        Key index and the row's hit or miss label.
    """
    if index < evaluable // 2 * 2:
        return index // 2, "hit" if index % 2 == 0 else "miss"
    return 0, "hit"


def _sighting(label: CacheResult, is_correct: bool) -> ChildSighting:
    """Choose the child sighting that matches the predicted correctness.

    Returns
    -------
    ChildSighting
        The sighting value implied by the label and correctness.
    """
    if label == "hit":
        return "not_seen" if is_correct else "seen"
    return "seen" if is_correct else "not_seen"


def _row_runtime(label: CacheResult, is_correct: bool) -> float:
    """Choose the runtime that carries the row's prediction.

    Returns
    -------
    float
        Hit rows stay fast; miss rows are slow only when predicted correctly.
    """
    if label == "hit":
        return 0.25
    return 2.0 if is_correct else 0.5


@pytest.mark.parametrize(
    ("correct", "expected"),
    [
        (96, "supported"),
        (95, "supported"),
        (94, "inconclusive"),
        (91, "inconclusive"),
        (90, "inconclusive"),
        (89, "refuted"),
    ],
)
def test_agreement_verdict_boundaries(
    correct: int, expected: outcome_replay.H2Verdict
) -> None:
    """The verdict changes exactly at 95% and below 90%."""
    report = outcome_replay.evaluate_h2(_rows(100, correct, 2_000))

    assert report.evaluable_records == 100, (
        "agreement_verdict_boundaries: report.evaluable_records == 100"
    )
    assert report.verdict == expected, (
        "agreement_verdict_boundaries: report.verdict == expected"
    )


@pytest.mark.parametrize(
    ("evaluable", "attributed", "expected"),
    [
        (99, 2_000, "insufficient"),
        (101, 2_000, "supported"),
        (100, 1_999, "insufficient"),
        (100, 2_000, "supported"),
        (100, 2_001, "supported"),
    ],
)
def test_minimum_sample_boundaries(
    evaluable: int, attributed: int, expected: outcome_replay.H2Verdict
) -> None:
    """Both evidence minima are inclusive at their documented values."""
    correct = round(evaluable * 0.95)
    report = outcome_replay.evaluate_h2(_rows(evaluable, correct, attributed))

    assert report.verdict == expected, (
        "minimum_sample_boundaries: report.verdict == expected"
    )
    if evaluable == 101:
        assert report.evaluable_records == 101, (
            "minimum_sample_boundaries: report.evaluable_records == 101"
        )


def test_threshold_constants_match_the_accepted_adr() -> None:
    """The implementation thresholds stay pinned to ADR 005's fixed text."""
    adr = _ADR.read_text(encoding="utf-8")

    assert outcome_replay.H2_SUPPORTED_AGREEMENT_PERCENT == 95, (
        "threshold_constants_match_t...: outcome_replay.H2_SUPPORTED_AGREEMEN"
    )
    assert outcome_replay.H2_REFUTED_BELOW_PERCENT == 90, (
        "threshold_constants_match_t...: outcome_replay.H2_REFUTED_BELOW_PERC"
    )
    assert outcome_replay.H2_MIN_EVALUABLE_RECORDS == 100, (
        "threshold_constants_match_t...: outcome_replay.H2_MIN_EVALUABLE_RECO"
    )
    assert outcome_replay.MIN_ATTRIBUTED_OUTCOMES == 2_000, (
        "threshold_constants_match_t...: outcome_replay.MIN_ATTRIBUTED_OUTCOM"
    )
    normalized_adr = " ".join(adr.split())
    assert (
        "Report `supported` at 95% agreement or more, `refuted` below 90%, "
        "`inconclusive` between, and `insufficient` below 2,000 attributed "
        "outcomes or 100 evaluable records. Also report each clue's agreement."
        in normalized_adr
    ), "ADR D11 retains the complete fixed H2 threshold policy"


def test_single_outcome_keys_do_not_enter_the_evaluable_set() -> None:
    """A large history of sampled hits is insufficient without any misses."""
    records = tuple(
        _outcome(
            _key(index),
            "hit",
            runtime=0.25,
            sighting="not_seen",
        )
        for index in range(2_000)
    )

    report = outcome_replay.evaluate_h2(records)

    assert report.attributed_outcomes == 2_000, (
        "single_outcome_keys_do_not_...: report.attributed_outcomes == 2_000"
    )
    assert report.evaluable_records == 0, (
        "single_outcome_keys_do_not_...: report.evaluable_records == 0"
    )
    assert report.verdict == "insufficient", (
        'single_outcome_keys_do_not_...: report.verdict == "insufficient"'
    )


def test_runtime_uses_only_earlier_hits_and_ambiguous_child_is_not_a_clue() -> None:
    """The strict runtime threshold follows the median of earlier hit rows."""
    key = _key(1)
    rows = (
        _outcome(key, "hit", runtime=0.2, sighting="ambiguous"),
        _outcome(key, "hit", runtime=0.4, sighting="unavailable"),
        _outcome(key, "hit", runtime=0.6, sighting="not_seen"),
        _outcome(key, "miss", runtime=1.21, sighting="ambiguous"),
        _outcome(key, "miss", runtime=1.2, sighting="seen"),
    )

    report = outcome_replay.evaluate_h2(rows)

    assert report.evaluable_records == 5, (
        "the key has both labels and every row was sampled"
    )
    assert report.combined_correct == 5, "1.21 exceeds the median-based 1.2 threshold"
    assert report.child_evaluable_records == 2, "ambiguous and unavailable are excluded"
    assert report.ambiguous_records == 2, (
        "runtime_uses_only_earlier_h...: report.ambiguous_records == 2"
    )
    assert report.unavailable_records == 1, (
        "runtime_uses_only_earlier_h...: report.unavailable_records == 1"
    )
    assert report.misses_seen == 1, (
        "runtime_uses_only_earlier_h...: report.misses_seen == 1"
    )
    assert report.misses_with_child_observation == 1, (
        "runtime_uses_only_earlier_h...: report.misses_with_child_observation"
    )
    assert report.miss_sighting_share == pytest.approx(1.0), (
        "runtime_uses_only_earlier_h...: report.miss_sighting_share == pytest"
    )


def test_reports_state_the_covered_span_and_the_sample_limits() -> None:
    """A verdict states how much history it read and the limits it inherits."""
    records = (
        _outcome(_key(1), "hit", runtime=0.25),
        dc.replace(
            _outcome(_key(1), "miss", runtime=2.0),
            observed_at=OBSERVED_AT + dt.timedelta(hours=3),
        ),
    )

    report = outcome_replay.evaluate_h2(records)

    assert report.as_dict()["covered"] == {
        "from": "2026-10-24T12:00:00Z",
        "until": "2026-10-24T15:00:00Z",
    }, "the report states the time span the store covers (R8)"
    assert outcome_replay.evaluate_h2(()).as_dict()["covered"] == {
        "from": None,
        "until": None,
    }, "an empty store reports no span rather than a fabricated one"
    assert report.as_dict()["caveats"] == list(outcome_evidence.H2_LIMITS), (
        "the H2 report states the quiet-window and sighting limits"
    )
    assert f"{CHILD_SAMPLE_SECONDS:g} s" in outcome_evidence.CHILD_SIGHTING_CAVEAT, (
        "the stated interval matches the sampler's cadence (R9)"
    )


def test_store_cli_text_and_json_snapshots_include_semantic_sample_sizes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    snapshot: SnapshotAssertion,
) -> None:
    """The H2 CLI snapshots retain verdict, sample sizes and clue counts."""
    path = tmp_path / "outcomes.jsonl"
    rows = _rows(
        100,
        95,
        2_000,
        status_overrides={0: "ambiguous", 1: "unavailable"},
    )
    outcome_store_io.replace_store(
        path,
        StoreHeader("fixed-salt", dt.datetime(2026, 10, 24, tzinfo=dt.UTC)),
        rows,
    )

    text_status = outcome_replay_cli.main(["h2", "--store", str(path)])
    text_output = capsys.readouterr().out

    assert text_status == 0, "store_cli_text_and_json_sna...: text_status == 0"
    assert "supported" in text_output, (
        'store_cli_text_and_json_sna...: "supported" in text_output'
    )
    assert "100 evaluable of 2000 attributed" in text_output, (
        'store_cli_text_and_json_sna...: "100 evaluable of 2000 attributed" i'
    )
    assert "covered 2026-10-24T12:00:00Z to 2026-10-24T12:00:00Z" in text_output, (
        "the text report states the covered span"
    )
    assert "caveat: labels come from solo and unanimous windows" in text_output, (
        "the text report states the quiet-window limit"
    )
    assert text_output == snapshot(name="h2-text"), (
        'store_cli_text_and_json_sna...: text_output == snapshot(name="h2-tex'
    )

    json_status = outcome_replay_cli.main(["h2", "--store", str(path), "--json"])
    json_output = capsys.readouterr().out
    report = json.loads(json_output)

    assert json_status == 0, "store_cli_text_and_json_sna...: json_status == 0"
    assert report["verdict"] == "supported", (
        'store_cli_text_and_json_sna...: report["verdict"] == "supported"'
    )
    assert report["evaluable_records"] == 100, (
        'store_cli_text_and_json_sna...: report["evaluable_records"] == 100'
    )
    assert report["attributed_outcomes"] == 2_000, (
        'store_cli_text_and_json_sna...: report["attributed_outcomes"] == 2_0'
    )
    assert report["child_sightings"]["ambiguous"] == 1, (
        'store_cli_text_and_json_sna...: report["child_sightings"]["ambiguous'
    )
    assert report["child_sightings"]["unavailable"] == 1, (
        'store_cli_text_and_json_sna...: report["child_sightings"]["unavailab'
    )
    assert report["covered"] == {
        "from": "2026-10-24T12:00:00Z",
        "until": "2026-10-24T12:00:00Z",
    }, "the JSON report carries the covered span"
    assert report["caveats"] == list(outcome_evidence.H2_LIMITS), (
        "the JSON report states both sample limits"
    )
    assert json_output == snapshot(name="h2-json"), (
        'store_cli_text_and_json_sna...: json_output == snapshot(name="h2-jso'
    )


def test_missing_h2_store_reports_insufficient_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An absent store yields an H2 insufficient report without an error exit."""
    status = outcome_replay_cli.main([
        "h2",
        "--store",
        str(tmp_path / "missing.jsonl"),
        "--json",
    ])
    report = json.loads(capsys.readouterr().out)

    assert status == 0, "missing_h2_store_reports_in...: status == 0"
    assert report["hypothesis"] == "h2", (
        'missing_h2_store_reports_in...: report["hypothesis"] == "h2"'
    )
    assert report["verdict"] == "insufficient", (
        'missing_h2_store_reports_in...: report["verdict"] == "insufficient"'
    )
    assert report["evaluable_records"] == 0, (
        'missing_h2_store_reports_in...: report["evaluable_records"] == 0'
    )
    assert report["covered"] == {"from": None, "until": None}, (
        "a missing store reports no span"
    )
    assert report["caveats"] == list(outcome_evidence.H2_LIMITS), (
        "a missing store still states the limits"
    )
