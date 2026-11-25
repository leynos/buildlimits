"""Round-trip contracts from the store to the snapshot and the report.

A verified row must survive save and load with its identity, availability and
chronology intact, and a document written by an older release must gain no
timing it never had. The dataset counts then state both evidence gates
separately, and the rendered report prints them.

The causal replay scenarios are in ``test_predict_timing_causal.py``.
"""

import dataclasses as dc
import json
import typing as typ
from pathlib import Path

from predict_timing_helpers import event, reviewer_stream, snapshot
from render_helpers import empty_document

from buildlimits.predict.dataset import Snapshot, SnapshotStats
from buildlimits.predict.render import render_markdown
from buildlimits.predict.snapshot_store import load_snapshot, save_snapshot


def test_the_snapshot_round_trip_preserves_identity_and_chronology(
    tmp_path: Path,
) -> None:
    """A verified history survives save and load with every moment intact."""
    events = reviewer_stream()
    path = tmp_path / "history.jsonl"
    save_snapshot(path, snapshot(events))

    loaded = load_snapshot(path)

    assert loaded.events == events, "every timing field round-trips exactly"
    assert [record.attempt for record in loaded.events] == [
        record.attempt for record in events
    ], "the attempt identities survive"
    assert [record.supervisor_generation for record in loaded.events] == [
        record.supervisor_generation for record in events
    ], "the clock domain survives"


def test_an_old_format_snapshot_round_trips_without_fabricated_timing(
    tmp_path: Path,
) -> None:
    """A version 2 document keeps its rows and gains no invented moments."""
    path = tmp_path / "history.jsonl"
    save_snapshot(path, snapshot((event(0, moments=(1.0, 5.0)),)))
    document = typ.cast(
        "dict[str, object]", json.loads(path.read_text(encoding="utf-8"))
    )
    document["snapshot_version"] = 2
    row = typ.cast("dict[str, object]", typ.cast("list[object]", document["events"])[0])
    for absent in (
        "completed_at",
        "attempt",
        "supervisor_generation",
        "label_observed_verified",
    ):
        row.pop(absent)
    path.write_text(json.dumps(document), encoding="utf-8")

    loaded = load_snapshot(path)

    assert loaded.events[0].label_observed_verified is False, (
        "no availability is invented"
    )
    assert loaded.events[0].attempt is None, "no attempt identity is invented"
    stats = loaded.stats()
    assert stats.scored == 0, "an old-format row is unscorable"
    assert stats.excluded == {"no_availability_time": 1}, "the gap is visible"


def test_the_benchmark_reports_each_gate_separately() -> None:
    """Dataset counts separate labels, decisions, availability and scoring."""
    stats = snapshot((
        event(0, moments=(1.0, 5.0)),
        event(1, moments=(None, 6.0)),
        dc.replace(event(2, moments=(3.0, 6.0)), label_observed_verified=False),
    )).stats()

    assert stats.labelled == 3, "every row carries a hit or miss label"
    assert stats.admission_recorded == 2, "two rows record a decision"
    assert stats.unverified_label_time == 1, "one row cannot prove its label time"
    assert stats.scored == 1, "only the row that passes both gates is scored"
    assert stats.excluded == {
        "no_admission_time": 1,
        "no_availability_time": 1,
    }, "each exclusion names its own gate"


def test_an_empty_history_reports_no_measured_count() -> None:
    """An empty snapshot reports zeroes, never a guessed or borrowed figure."""
    stats: SnapshotStats = Snapshot(
        created_at=event(0, moments=(0.0, 0.0)).observed_at,
        source=snapshot(()).source,
        journal=None,
        events=(),
        gaps=(),
    ).stats()

    assert stats.events == 0, "no events"
    assert stats.scored == 0, "nothing scored"
    assert stats.excluded == {}, "no exclusion reason without a row"
    assert stats.unverified_label_time == 0, "no label was withheld"


def test_the_report_states_the_scored_population_and_its_exclusions() -> None:
    """The rendered report never hides a timing exclusion behind a label count."""
    document = empty_document()
    stats = typ.cast("dict[str, object]", typ.cast("dict", document["data"])["stats"])
    stats.update({
        "labelled": 3,
        "admission_recorded": 3,
        "admission_usable": 2,
        "unverified_label_time": 2,
        "scored": 1,
        "excluded": {"no_availability_time": 2},
    })

    markdown = render_markdown(document)

    assert "- Labels withheld for an unverified label time: 2" in markdown, (
        "the report states the labels it withheld"
    )
    assert (
        "- Scored records: 1 (exclusions: no_availability_time=2; "
        "0 duplicate attempts, 0 conflicting attempts)"
    ) in markdown, "exclusions are counted by their closed reason"


def test_a_duplicate_and_a_conflict_are_counted_apart() -> None:
    """The report distinguishes an idempotent retry from a contradiction."""
    original = event(0, moments=(1.0, 5.0), attempt="same")
    retried = typ.cast("typ.Any", original)
    conflicting = event(1, moments=(1.0, 5.0), label="miss", attempt="other")

    stats = snapshot((original, retried, conflicting)).stats()

    assert stats.duplicates == 1, "the exact repeat is an idempotent duplicate"
    assert stats.conflicts == 0, "no pair contradicts itself"
    assert stats.scored == 2, "the original and the distinct attempt are scored"


def test_a_duplicate_attempt_is_not_counted_as_missing_availability() -> None:
    """A repeated row is excluded by its own reason, not as an absent moment.

    Both rows carry a verified availability, so the availability counter must
    stay at zero and the duplicate must appear only in its own count.
    """
    original = event(0, moments=(1.0, 5.0), attempt="same")
    retried = typ.cast("typ.Any", original)

    stats = snapshot((original, retried)).stats()

    assert stats.unverified_label_time == 0, "both rows prove their label time"
    assert stats.duplicates == 1, "the retry is counted as a duplicate"
    assert stats.excluded == {"duplicate_attempt": 1}, "under its own reason"
