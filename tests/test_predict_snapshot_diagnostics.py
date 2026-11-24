"""Snapshot diagnostics and version-compatibility contracts.

The loader must accept the documents earlier releases wrote while refusing a
diagnostics block that cannot describe a possible history. These tests keep
those two directions together, apart from the general document-shape tests.
"""

import dataclasses as dc
import datetime as dt
import typing as typ
from pathlib import Path

import pytest
from outcome_test_helpers import OBSERVED_AT, attributed_record
from predict_test_helpers import ACTIVE_FILE_ONLY, write_json

from buildlimits.outcome_record_format import DecodedStore, StoreHeader
from buildlimits.predict.accounting import HistoryLoss
from buildlimits.predict.dataset import StoreHistory, build_snapshot
from buildlimits.predict.records import decode_history
from buildlimits.predict.snapshot_codec import snapshot_to_dict
from buildlimits.predict.snapshot_store import load_snapshot

NOW = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)


def _history(*records: object) -> StoreHistory:
    """Decode records into the history value a snapshot is built from."""
    decoded = DecodedStore(StoreHeader("fixed-salt", NOW), typ.cast("typ.Any", records))
    return decode_history(decoded)


def _store_block(document: dict[str, object]) -> dict[str, object]:
    """Return the mutable diagnostics store block of one encoded document."""
    return typ.cast(
        "dict[str, object]",
        typ.cast("dict[str, object]", document["diagnostics"])["store"],
    )


def _document() -> dict[str, object]:
    """Return the encoded snapshot of one attributed hit, without a journal."""
    return snapshot_to_dict(
        build_snapshot(
            _history(attributed_record(outcome="hit")),
            now=NOW,
            history_loss=ACTIVE_FILE_ONLY,
        )
    )


def test_a_version_one_snapshot_loads_without_diagnostics(tmp_path: Path) -> None:
    """A snapshot written before the accounting block remains readable."""
    document = _document()
    document["snapshot_version"] = 1
    document.pop("diagnostics")

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert loaded.diagnostics is None, "the absent block is reported as unavailable"


def test_a_version_two_snapshot_round_trips_its_diagnostics(tmp_path: Path) -> None:
    """The retained-store and history-loss counts survive the document."""
    snapshot = build_snapshot(
        _history(
            attributed_record(outcome="hit"),
            attributed_record(outcome="miss"),
        ),
        now=NOW,
        history_loss=ACTIVE_FILE_ONLY,
    )
    path = write_json(tmp_path / "history.jsonl", snapshot_to_dict(snapshot))

    loaded = load_snapshot(path)

    assert loaded.diagnostics == snapshot.diagnostics, "the counts survive"
    assert loaded.diagnostics is not None, "the block is present"
    assert loaded.diagnostics.store.outcome_records == 2, "both rows are counted"
    assert loaded.diagnostics.store.labels == 2, "both rows are labels"


def test_a_version_one_event_may_omit_admission(tmp_path: Path) -> None:
    """A version 1 row written before the field existed still loads."""
    document = _document()
    document["snapshot_version"] = 1
    document.pop("diagnostics")
    event = typ.cast(
        "dict[str, object]", typ.cast("list[object]", document["events"])[0]
    )
    event.pop("admitted_at")

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert loaded.events[0].admitted_at is None, "the absent time stays unknown"


def test_a_version_two_event_must_name_admission(tmp_path: Path) -> None:
    """A version 2 row keeps the exact event shape, including admission."""
    document = _document()
    event = typ.cast(
        "dict[str, object]", typ.cast("list[object]", document["events"])[0]
    )
    event.pop("admitted_at")

    with pytest.raises(ValueError, match="snapshot fields"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_an_impossible_cacheable_intersection_is_refused(tmp_path: Path) -> None:
    """A labelled and cacheable pair must share at least one outcome row."""
    document = _document()
    store = _store_block(document)
    store["cacheable_labels"] = 0

    with pytest.raises(ValueError, match="possible intersection"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_an_irreconcilable_diagnostics_block_is_refused(tmp_path: Path) -> None:
    """A count that disagrees with its breakdown cannot become evidence."""
    document = _document()
    store = _store_block(document)
    store["outcome_records"] = 7

    with pytest.raises(ValueError, match="do not reconcile"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def _events(document: dict[str, object]) -> list[dict[str, object]]:
    """Return the mutable event rows of one encoded document."""
    return typ.cast("list[dict[str, object]]", document["events"])


def test_a_dropped_event_is_refused(tmp_path: Path) -> None:
    """The event stream must hold one row per retained outcome."""
    document = _document()
    _events(document).clear()

    with pytest.raises(ValueError, match="events but its diagnostics"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_a_swapped_label_is_refused(tmp_path: Path) -> None:
    """Matching label totals cannot hide contradictory hit and miss counts."""
    document = _document()
    _events(document)[0]["label"] = "miss"

    with pytest.raises(ValueError, match="hit events but its diagnostics"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_a_removed_admission_time_is_refused(tmp_path: Path) -> None:
    """An event's admission presence must match the recorded coverage."""
    adopted = dc.replace(
        attributed_record(outcome="hit"),
        admitted_at=OBSERVED_AT - dt.timedelta(seconds=2),
    )
    document = snapshot_to_dict(
        build_snapshot(
            _history(adopted),
            now=NOW,
            history_loss=ACTIVE_FILE_ONLY,
        )
    )
    _events(document)[0]["admitted_at"] = None

    with pytest.raises(ValueError, match="admission times on its events"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_diagnostics_labels_must_describe_the_events(tmp_path: Path) -> None:
    """A label count that disagrees with the events names no single history.

    The block stays internally consistent — one outcome row, no labels — so
    only the join to the event stream can refuse it.
    """
    document = _document()
    store = _store_block(document)
    store["labels"] = 0
    store["cacheable_labels"] = 0
    store["by_result"] = {"unattributed": 1}

    with pytest.raises(ValueError, match="hit events but its diagnostics"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_diagnostics_must_describe_the_source_store(tmp_path: Path) -> None:
    """The two row totals in one snapshot cannot name different stores."""
    document = _document()
    source = typ.cast("dict[str, object]", document["source"])
    source["records_total"] = 2
    source["outcome_records"] = 2

    with pytest.raises(ValueError, match="disagree with its source"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_an_unavailable_inventory_survives_the_snapshot(tmp_path: Path) -> None:
    """A failed inventory scan is distinct from a measured active-only read."""
    snapshot = build_snapshot(
        _history(attributed_record(outcome="hit")),
        now=NOW,
        history_loss=HistoryLoss(active_only=False, inventory_unavailable=True),
    )
    path = write_json(tmp_path / "history.jsonl", snapshot_to_dict(snapshot))

    loaded = load_snapshot(path)

    assert loaded.diagnostics is not None, "the block is present"
    assert loaded.diagnostics.history.inventory_unavailable is True, (
        "the failure is recorded"
    )
    assert loaded.diagnostics.history.active_only is False, (
        "the read was not active-only"
    )
