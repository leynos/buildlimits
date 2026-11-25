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
from buildlimits.predict.diagnostics_generations import (
    require_generation_labels,
    unrecorded_generations,
)
from buildlimits.predict.records import decode_history
from buildlimits.predict.snapshot_codec import snapshot_to_dict
from buildlimits.predict.snapshot_store import load_snapshot

NOW = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)
_PROVENANCE_VERSION = 3
"""The first snapshot version whose events state a label-time provenance."""
_GENERATION_VERSION = 4
"""The first snapshot version whose diagnostics state a per-generation map."""


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


def _as_version(document: dict[str, object], version: int) -> dict[str, object]:
    """Rewrite a current document as the shape the named version wrote.

    A test that loads an older document must reproduce what that release really
    wrote, not a current row with a downgraded header. Version 1 or 2 predates
    the label-time provenance marker, so the marker is dropped with the header;
    version 3 states the marker but predates the per-generation breakdown, so
    that map is dropped; the current version states both, so it keeps them.
    """
    document["snapshot_version"] = version
    if version < _PROVENANCE_VERSION:
        for event in _events(document):
            event.pop("label_observed_verified", None)
    if version < _GENERATION_VERSION:
        block = document.get("diagnostics")
        if isinstance(block, dict):
            store = typ.cast("dict[str, object]", block).get("store")
            if isinstance(store, dict):
                mutable = typ.cast("dict[str, object]", store)
                mutable.pop("by_generation", None)
                mutable.pop("labels_by_generation", None)
    return document


def test_a_version_one_snapshot_loads_without_diagnostics(tmp_path: Path) -> None:
    """A snapshot written before the accounting block remains readable."""
    document = _as_version(_document(), 1)
    document.pop("diagnostics")

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert loaded.diagnostics is None, "the absent block is reported as unavailable"


def test_an_older_event_time_is_never_read_as_verified(tmp_path: Path) -> None:
    """A version 1 or 2 snapshot carries no proof of when its label was known."""
    for version in (1, 2):
        document = _as_version(_document(), version)
        document.pop("diagnostics")

        loaded = load_snapshot(write_json(tmp_path / f"v{version}.jsonl", document))

        assert loaded.events[0].label_observed_verified is False, (
            f"version {version} predates the marker, so its time is a lower bound"
        )


@pytest.mark.parametrize("version", [2, 3, 4])
def test_a_versioned_snapshot_round_trips_its_diagnostics(
    tmp_path: Path, version: int
) -> None:
    """The retained-store and history-loss counts survive each document shape.

    Version 2 predates the label-time provenance marker, version 3 states it,
    and version 4 adds the per-generation breakdown, so a round trip must be
    exercised at each shape rather than at the current one alone.
    """
    snapshot = build_snapshot(
        _history(
            attributed_record(outcome="hit"),
            attributed_record(outcome="miss"),
        ),
        now=NOW,
        history_loss=ACTIVE_FILE_ONLY,
    )
    document = _as_version(snapshot_to_dict(snapshot), version)
    path = write_json(tmp_path / f"v{version}.jsonl", document)

    loaded = load_snapshot(path)

    assert loaded.diagnostics == snapshot.diagnostics, "the counts survive"
    assert loaded.diagnostics is not None, "the block is present"
    assert loaded.diagnostics.store.outcome_records == 2, "both rows are counted"
    assert loaded.diagnostics.store.labels == 2, "both rows are labels"


def test_a_version_one_event_may_omit_admission(tmp_path: Path) -> None:
    """A version 1 row written before the field existed still loads."""
    document = _as_version(_document(), 1)
    document.pop("diagnostics")
    _events(document)[0].pop("admitted_at")

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert loaded.events[0].admitted_at is None, "the absent time stays unknown"


def test_a_version_two_event_must_name_admission(tmp_path: Path) -> None:
    """A version 2 row keeps the exact event shape, including admission."""
    document = _as_version(_document(), 2)
    _events(document)[0].pop("admitted_at")

    with pytest.raises(ValueError, match="snapshot fields"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_a_version_three_event_must_state_its_label_time_provenance(
    tmp_path: Path,
) -> None:
    """A current row that omits the marker does not masquerade as proof."""
    document = _document()
    _events(document)[0].pop("label_observed_verified")

    with pytest.raises(ValueError, match="snapshot fields"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_a_label_time_marker_that_is_not_a_boolean_is_refused(
    tmp_path: Path,
) -> None:
    """A marker outside true/false cannot be read as either."""
    document = _document()
    _events(document)[0]["label_observed_verified"] = "true"

    with pytest.raises(TypeError, match="must be a boolean"):
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
    # The per-generation maps partition the same two totals, so a block that
    # says it holds no labels must also say none of its rows were labelled.
    # Leaving them contradicting `labels` would refuse the block before the
    # event join this test is about could run.
    store["labels_by_generation"] = {}

    with pytest.raises(ValueError, match="hit events but its diagnostics"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def _two_origin_document() -> dict[str, object]:
    """Return a document whose two events carry distinct origins."""
    first = attributed_record(outcome="hit")
    second = dc.replace(attributed_record(outcome="miss"), origin="git")
    return snapshot_to_dict(
        build_snapshot(
            _history(first, second),
            now=NOW,
            history_loss=ACTIVE_FILE_ONLY,
        )
    )


def test_a_document_whose_origins_reconcile_is_accepted(tmp_path: Path) -> None:
    """The unchanged document is the control: both origin counts are right."""
    loaded = load_snapshot(
        write_json(tmp_path / "history.jsonl", _two_origin_document())
    )

    assert loaded.diagnostics is not None, "the block is present"
    assert loaded.diagnostics.store.by_origin == {"git": 1, "registry": 1}, (
        "one event of each origin"
    )


def test_a_changed_event_origin_is_refused(tmp_path: Path) -> None:
    """Changing one event's origin names no single history.

    The label counts, the admission counts and the row totals are untouched,
    so only a per-origin comparison can refuse this document.
    """
    document = _two_origin_document()
    git_event = next(event for event in _events(document) if event["origin"] == "git")
    git_event["origin"] = "registry"

    with pytest.raises(ValueError, match="records its events by origin"):
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


@pytest.mark.parametrize(
    ("rows", "labelled", "expected"),
    [
        pytest.param({"a": 1}, {"b": 1}, "name unknown buckets", id="unknown-bucket"),
        pytest.param(
            {"a": 1},
            {"a": 2},
            "exceed their own generation totals",
            id="labels-exceed-rows",
        ),
        pytest.param(
            {"a": 1, "unrecorded": 1},
            {"a": 1},
            "do not sum to the labelled count",
            id="labels-do-not-sum",
        ),
    ],
)
def test_a_generation_label_map_that_cannot_describe_a_history_is_refused(
    rows: dict[str, int], labelled: dict[str, int], expected: str
) -> None:
    """Each way the labelled map can contradict its document is refused.

    The labelled map partitions the labelled rows while the row map partitions
    all of them, so a document that breaks either relationship states two
    histories at once. A reader that printed it would report counts no single
    store ever held.
    """
    # The document's own labelled total is one more than the map states, which
    # is the only way the sum rule can be the one that fires.
    declared = sum(labelled.values()) + (expected.endswith("labelled count"))
    with pytest.raises(ValueError, match=expected):
        require_generation_labels(labelled, rows, labels=declared, label="doc")


def test_a_generation_label_map_that_reconciles_is_accepted() -> None:
    """The control case proves the refusals above come from the maps.

    Without it, a check that refused every labelled map would satisfy each
    refusal while making the dimension unreadable.
    """
    require_generation_labels(
        {"a": 1}, {"a": 2, "unrecorded": 1}, labels=1, label="doc"
    )


def test_a_pre_generation_history_states_all_of_its_labels_as_unrecorded() -> None:
    """An older document's fallback accounts for its labels, not just its rows.

    A history that names no generation cannot say which generation a label came
    from either, so both maps have to carry the same bucket. Dropping the
    labelled map instead would leave it failing to sum to the labelled total.
    """
    rows, labelled = unrecorded_generations(outcome_records=4, labels=3)

    assert rows == {"unrecorded": 4}, "every row is unattributed"
    assert labelled == {"unrecorded": 3}, "every label is unattributed too"
    require_generation_labels(labelled, rows, labels=3, label="doc")


def test_an_empty_history_states_no_generation_buckets() -> None:
    """A store with no rows gets no fabricated bucket at all."""
    assert unrecorded_generations(outcome_records=0, labels=0) == ({}, {}), (
        "an empty store names no bucket rather than an empty one"
    )
