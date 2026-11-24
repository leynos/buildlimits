"""The benchmark's default gates, measured on a realistic-sized history.

The label gate and the admission gate are separate. A history can hold enough
labelled rows to run and still carry no admission time, in which case nothing
could have been predicted and the run says so rather than scoring a label that
was already known. These tests hold the label count at the packaged default
threshold and vary only the admission evidence.
"""

import dataclasses as dc
import datetime as dt
import json
import typing as typ
from pathlib import Path

import pytest
from outcome_test_helpers import OBSERVED_AT, cache_key
from predict_test_helpers import benchmark_argv, outcome_record, read_results

from buildlimits.outcome_record_encode import encode_line
from buildlimits.outcome_record_format import OutcomeRecord, StoreHeader
from buildlimits.predict import benchmark_cli, dataset_cli

LABELS = 2000
"""The packaged ``minimum_labelled_events``, restated as this test's size."""

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


def write_labelled_history(path: Path, count: int, *, with_admission: bool) -> None:
    """Write one labelled store that mixes version 1 and version 2 row shapes.

    Without ``with_admission`` the store alternates the two shapes and states
    no admission time at all: a version 1 row omits the fields, and a version 2
    row carries an explicit null. With it, every row is version 2 and carries
    an admission moment two seconds before its own label.

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


def _snapshot_and_benchmark(
    tmp_path: Path, *, with_admission: bool
) -> tuple[dict[str, typ.Any], str]:
    """Extract, snapshot and benchmark one history at the packaged defaults.

    Returns
    -------
    tuple[dict[str, object], str]
        The result document and the rendered Markdown.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    snapshot = tmp_path / "history.jsonl"
    output = tmp_path / "out"
    write_labelled_history(store, LABELS, with_admission=with_admission)
    extracted = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(snapshot),
        "--skip-journal",
    ])
    assert extracted == 0, "the extraction reads the mixed history"
    status = benchmark_cli.main(benchmark_argv(snapshot, output))
    assert status == 0, "an honest gate outcome is not a command failure"
    return read_results(output)


def _require_default_label_gate(document: dict[str, typ.Any]) -> None:
    """Require the run to have used the packaged label gate unchanged.

    Both runs must clear the same default minimum, so a status that differs
    between them is the admission evidence's doing rather than a weaker gate.
    """
    assert document["config"]["minimum_labelled_events"] == LABELS, (
        "the packaged default gate is the one being tested"
    )
    assert document["data"]["stats"]["labelled"] == LABELS, "the label gate is met"


@pytest.mark.timeout(120)
def test_two_thousand_labels_without_admission_report_the_missing_gate(
    tmp_path: Path,
) -> None:
    """The default label gate is cleared, and the admission gate is what stops.

    ``minimum_labelled_events`` is compared against labelled rows alone, so this
    history passes it. Every row then lacks an admission time, so no event
    could have been predicted and the run reports that rather than scoring a
    label that arrived before its own decision could be taken.
    """
    document, markdown = _snapshot_and_benchmark(tmp_path, with_admission=False)

    _require_default_label_gate(document)
    assert document["data"]["stats"]["admission_usable"] == 0, "no row is predictable"
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
    document, _markdown = _snapshot_and_benchmark(tmp_path, with_admission=True)

    _require_default_label_gate(document)
    stats = typ.cast("dict[str, object]", document["data"]["stats"])
    assert stats["admission_usable"] == LABELS, "every row is predictable"
    assert document["status"] == "ok", "the history now supports a verdict"
    assert document["replay"]["moments"] > 0, "predictions were made at admission"
    scored = [entry["held-out"]["scored"] for entry in document["predictors"].values()]
    assert any(scored), "the held-out rows are scored rather than only counted"
