"""The benchmark's default gates, measured on a realistic-sized history.

The label gate, the admission gate and the held-out gate are separate. A
history can hold enough labelled rows to run and still carry too little
admission evidence, in which case nothing (or too little) could have been
predicted and the run says so rather than scoring a label that was already
known. These tests hold the label count at the packaged default threshold and
vary only the admission evidence, so a status that differs between them is the
evidence's doing rather than a weaker gate.

A store-derived row is unverified by design: a version 2 row cannot prove when
its label became known, so the replay withholds it. These tests therefore build
verified snapshot events to exercise the admission and held-out gates, and keep
one store-derived case to prove the withholding is what a real host history
meets.
"""

import dataclasses as dc
import datetime as dt
import json
import typing as typ
from pathlib import Path

import pytest
from outcome_test_helpers import OBSERVED_AT, cache_key
from predict_test_helpers import (
    benchmark_argv,
    hand_event,
    outcome_record,
    read_results,
)

from buildlimits.outcome_record_encode import encode_line
from buildlimits.outcome_record_format import OutcomeRecord, StoreHeader
from buildlimits.predict import benchmark_cli, dataset_cli
from buildlimits.predict.dataset import Snapshot, Source
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.journal_provenance import JournalUnavailable
from buildlimits.predict.snapshot_store import save_snapshot

LABELS = 2000
"""The packaged ``minimum_labelled_events``, restated as this test's size."""

STEP = dt.timedelta(seconds=30)
LABEL_DELAY = dt.timedelta(seconds=2)

# A version 1 row predates admission and classification evidence, so writing
# one means omitting exactly those fields rather than writing nulls.
_V1_OMITTED_FIELDS = ("outcome_reason", "outcome_attribution", "admitted_at")


def _labelled_record(index: int, *, with_admission: bool) -> OutcomeRecord:
    """Return one labelled, cacheable record at a distinct label time.

    Returns
    -------
    OutcomeRecord
        The record, with its identity distinct from every other row's.
    """
    record = outcome_record(
        index,
        "hit" if index % 2 else "miss",
        dc.replace(cache_key(), argument_digest=f"{index:064x}"),
    )
    return dc.replace(
        record,
        outcome_reason="none",
        outcome_attribution="solo",
        admitted_at=(
            record.observed_at - dt.timedelta(seconds=2) if with_admission else None
        ),
    )


def _labelled_events(count: int, *, usable: int) -> tuple[Event, ...]:
    """Return ``count`` verified events, ``usable`` of them predictable.

    Every event carries a hit or miss label whose time is proven, so the label
    gate counts all of them. Only the first ``usable`` events carry an
    admission time at or before their label; the rest carry a time after it,
    which makes them labelled but unpredicted.

    Parameters
    ----------
    count : int
        How many labelled events to build.
    usable : int
        How many of them carry a usable admission time.

    Returns
    -------
    tuple[Event, ...]
        The events, in stream order.
    """
    events = []
    for index in range(count):
        observed = EPOCH + index * STEP
        admitted = observed - LABEL_DELAY if index < usable else observed + LABEL_DELAY
        events.append(
            hand_event(
                index,
                admitted_at=admitted,
                observed_at=observed,
                label="hit" if index % 2 else "miss",
            )
        )
    return tuple(events)


def _verified_snapshot(tmp_path: Path, *, usable: int) -> Path:
    """Write a snapshot of ``LABELS`` verified events, ``usable`` predictable.

    Returns
    -------
    Path
        The snapshot the benchmark can be pointed at.
    """
    events = _labelled_events(LABELS, usable=usable)
    snapshot = Snapshot(
        created_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
        source=Source(
            salt_id="verified",
            written_at=OBSERVED_AT,
            records_total=LABELS,
            outcome_records=LABELS,
            summary_records=0,
        ),
        journal=JournalUnavailable("journal query skipped"),
        events=events,
        gaps=(),
    )
    path = tmp_path / "verified.jsonl"
    save_snapshot(path, snapshot)
    return path


def _write_labelled_store(path: Path, count: int, *, with_admission: bool) -> None:
    """Write one labelled store that mixes version 1 and version 2 row shapes.

    Without ``with_admission`` the store alternates the two shapes and states
    no admission time at all. With it, every row is version 2 and carries an
    admission moment two seconds before its own label. Either way the rows are
    version 2 history, so the dataset command emits their label times as
    unverified.

    Parameters
    ----------
    path : Path
        The store file to write.
    count : int
        How many labelled rows to write.
    with_admission : bool
        Whether every row carries a usable admission time.
    """
    lines = [encode_line(StoreHeader("fixed-salt", OBSERVED_AT))]
    for index in range(count):
        line = encode_line(_labelled_record(index, with_admission=with_admission))
        if not with_admission and index % 2:
            document = json.loads(line)
            for field in _V1_OMITTED_FIELDS:
                document.pop(field)
            line = json.dumps(document, separators=(",", ":"), sort_keys=True)
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _run(snapshot: Path, output: Path) -> tuple[dict[str, typ.Any], str]:
    """Benchmark one snapshot and return its document and Markdown.

    Returns
    -------
    tuple[dict[str, object], str]
        The result document and the rendered Markdown.
    """
    status = benchmark_cli.main(benchmark_argv(snapshot, output))
    assert status == 0, "an honest gate outcome is not a command failure"
    return read_results(output)


def _stats(document: dict[str, typ.Any]) -> dict[str, object]:
    """Return the result document's mutable stats block.

    Returns
    -------
    dict[str, object]
        The snapshot's summary counts as the report recorded them.
    """
    return typ.cast("dict[str, object]", document["data"]["stats"])


def _gated_stats(document: dict[str, typ.Any]) -> dict[str, object]:
    """Require the packaged label gate unchanged, and return the stats.

    Every run must clear the same default minimum, so a status that differs
    between them is the admission evidence's doing rather than a weaker gate.

    Returns
    -------
    dict[str, object]
        The snapshot's summary counts, once the gate is confirmed met.
    """
    assert document["config"]["minimum_labelled_events"] == LABELS, (
        "the packaged default gate is the one being tested"
    )
    stats = _stats(document)
    assert stats["labelled"] == LABELS, "the label gate is met"
    return stats


@pytest.mark.timeout(120)
def test_a_store_history_is_withheld_because_its_label_times_are_unverified(
    tmp_path: Path,
) -> None:
    """A version 2 store row proves a label existed, not when it was known.

    The same store shape with and without admission times is withheld for the
    same reason: the format version cannot tell a row the fixed recorder dated
    from one the earlier build dated at its launch's completion. Treating the
    older row as verified would release its label before the reading that
    produced it, so every store-derived label is withheld and counted.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    output = tmp_path / "out"
    _write_labelled_store(store, LABELS, with_admission=True)
    extracted = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(tmp_path / "history.jsonl"),
        "--skip-journal",
    ])
    assert extracted == 0, "the extraction reads the history"

    document, markdown = _run(tmp_path / "history.jsonl", output)

    stats = _stats(document)
    assert stats["labelled"] == LABELS, "the rows are labelled"
    assert stats["unverified_label_time"] == LABELS, (
        "every store-derived label is unverified"
    )
    assert document["status"] == "insufficient data", (
        "an unverified label is not scored evidence"
    )
    assert "unverified" in document["verdict"], "the verdict names what it withheld"
    assert "unverified label time" in markdown, "the report states the withholding"


@pytest.mark.timeout(120)
def test_two_thousand_verified_labels_without_admission_report_the_missing_gate(
    tmp_path: Path,
) -> None:
    """The default label gate is cleared, and the admission gate is what stops.

    ``minimum_labelled_events`` is compared against labelled rows alone, so this
    history passes it. Every row then lacks a usable admission time, so no
    event could have been predicted and the run reports that rather than
    scoring a label that arrived before its own decision could be taken.
    """
    document, markdown = _run(_verified_snapshot(tmp_path, usable=0), tmp_path / "out")

    assert _gated_stats(document)["admission_usable"] == 0, "no row is predictable"
    assert document["status"] == "insufficient admission times", "the gate is named"
    assert "insufficient admission times" in document["verdict"], (
        "the verdict states the missing evidence"
    )
    assert "**insufficient admission times**" in markdown, "the report says so"


@pytest.mark.timeout(120)
def test_the_same_labels_with_admission_times_are_scored(tmp_path: Path) -> None:
    """The control: the same labels plus admission evidence are scored.

    Nothing about the label gate changed between the two runs, so a status that
    moved from insufficient admission times to scored rows is the admission
    evidence's doing.
    """
    document, _markdown = _run(
        _verified_snapshot(tmp_path, usable=LABELS), tmp_path / "out"
    )

    stats = _gated_stats(document)
    assert stats["admission_recorded"] == LABELS, "every row records an admission"
    assert stats["admission_usable"] == LABELS, "every row is predictable"
    assert document["status"] == "ok", "the history now supports a verdict"
    assert document["replay"]["moments"] > 0, "predictions were made at admission"
    scored = [entry["held-out"]["scored"] for entry in document["predictors"].values()]
    assert any(scored), "the held-out rows are scored rather than only counted"


@pytest.mark.timeout(120)
def test_a_few_usable_admission_times_do_not_stand_for_the_whole_history(
    tmp_path: Path,
) -> None:
    """A partly usable history reports what its evidence supports, not a verdict.

    Two thousand labels clear the label gate, and five usable admission times
    keep the run clear of ``insufficient admission times``. Five paired rows are
    still far below ``minimum_scored_events``, so no predictor was judged and
    the honest status is ``insufficient held-out evidence``. One usable row is
    not treated as enough, and a wholly unusable history is not the only
    shortage the report can name.
    """
    document, markdown = _run(_verified_snapshot(tmp_path, usable=5), tmp_path / "out")

    stats = _gated_stats(document)
    assert stats["admission_usable"] == 5, "only five rows are predictable"
    assert stats["admission_unusable"] == LABELS - 5, (
        "the rest carry an admission after their own label"
    )
    assert document["status"] == "insufficient held-out evidence", (
        "the label and admission gates are met, but the held-out one is not"
    )
    assert document["status"] != "ok", "five paired rows never become a verdict"
    assert "insufficient held-out evidence" in document["verdict"], (
        "the verdict names the missing evidence"
    )
    assert "**insufficient held-out evidence**" in markdown, "the report says so"
