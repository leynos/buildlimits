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
from outcome_test_helpers import (
    OBSERVED_AT,
    VERSION_ONE,
    VERSION_TWO,
    cache_key,
    older_document_row,
)
from predict_test_helpers import (
    benchmark_argv,
    hand_event,
    outcome_record,
    read_results,
)

from buildlimits.outcome_record_encode import encode_line
from buildlimits.outcome_record_format import (
    FORMAT_VERSION,
    OutcomeRecord,
    StoreHeader,
)
from buildlimits.predict import benchmark_cli, dataset_cli
from buildlimits.predict.dataset import Snapshot, Source
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.journal_provenance import JournalUnavailable
from buildlimits.predict.snapshot_store import save_snapshot

LABELS = 2000
"""The packaged ``minimum_labelled_events``, restated as this test's size."""

STEP = dt.timedelta(seconds=30)
LABEL_DELAY = dt.timedelta(seconds=2)

CURRENT_VERSION = FORMAT_VERSION
"""The store format whose rows carry the evidence this file compares."""


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


def _write_labelled_store(path: Path, count: int, *, with_admission: bool) -> None:
    """Write one labelled store with or without admission evidence.

    Without ``with_admission`` the store alternates the version 1 and version
    2 shapes and states no admission time at all, so every label time is
    unverified as well: the report withholds them rather than treating a
    recorded time as proof. With it, every row is written in the current
    shape and carries an admission moment two seconds before its own label.
    Only the admission evidence differs between the two runs, so a change of
    status is that evidence's doing.

    Parameters
    ----------
    path : Path
        The store file to write.
    count : int
        How many labelled rows to write.
    with_admission : bool
        Whether every row carries a usable admission time.
    """
    version = CURRENT_VERSION if with_admission else VERSION_TWO
    lines = [encode_line(StoreHeader("fixed-salt", OBSERVED_AT, version=version))]
    for index in range(count):
        record = _labelled_record(index, with_admission=with_admission)
        document = (
            json.loads(encode_line(record))
            if with_admission
            else older_document_row(
                record, version=VERSION_ONE if index % 2 else VERSION_TWO
            )
        )
        lines.append(json.dumps(document, separators=(",", ":"), sort_keys=True))
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
