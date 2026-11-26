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
from predict_diagnostics_helpers import (
    NOW,
    diagnostics_document,
    document_as_version,
    events_of,
    history_from_records,
    store_block,
)
from predict_test_helpers import ACTIVE_FILE_ONLY, write_json

from buildlimits.predict.accounting import HistoryLoss
from buildlimits.predict.dataset import build_snapshot
from buildlimits.predict.snapshot_codec import snapshot_to_dict
from buildlimits.predict.snapshot_store import load_snapshot


def test_a_version_one_snapshot_loads_without_diagnostics(tmp_path: Path) -> None:
    """A snapshot written before the accounting block remains readable."""
    document = document_as_version(diagnostics_document(), 1)
    document.pop("diagnostics")

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert loaded.diagnostics is None, "the absent block is reported as unavailable"


def test_an_older_event_time_is_never_read_as_verified(tmp_path: Path) -> None:
    """A version 1 or 2 snapshot carries no proof of when its label was known."""
    for version in (1, 2):
        document = document_as_version(diagnostics_document(), version)
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
        history_from_records(
            attributed_record(outcome="hit"),
            attributed_record(outcome="miss"),
        ),
        now=NOW,
        history_loss=ACTIVE_FILE_ONLY,
    )
    document = document_as_version(snapshot_to_dict(snapshot), version)
    path = write_json(tmp_path / f"v{version}.jsonl", document)

    loaded = load_snapshot(path)

    assert loaded.diagnostics == snapshot.diagnostics, "the counts survive"
    assert loaded.diagnostics is not None, "the block is present"
    assert loaded.diagnostics.store.outcome_records == 2, "both rows are counted"
    assert loaded.diagnostics.store.labels == 2, "both rows are labels"


def test_a_version_one_event_may_omit_admission(tmp_path: Path) -> None:
    """A version 1 row written before the field existed still loads."""
    document = document_as_version(diagnostics_document(), 1)
    document.pop("diagnostics")
    events_of(document)[0].pop("admitted_at")

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert loaded.events[0].admitted_at is None, "the absent time stays unknown"


def test_a_half_correlation_pair_is_refused(tmp_path: Path) -> None:
    """A row naming an attempt without its generation is not a correlation.

    The store writes the pair together or not at all, so a document stating one
    half describes a row no writer produced. Accepting it would let the event
    evade duplicate and conflict detection while appearing correlated.
    """
    document = diagnostics_document()
    events_of(document)[0]["attempt"] = "g1"

    with pytest.raises(ValueError, match="stated together"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_a_version_two_event_must_name_admission(tmp_path: Path) -> None:
    """A version 2 row keeps the exact event shape, including admission."""
    document = document_as_version(diagnostics_document(), 2)
    events_of(document)[0].pop("admitted_at")

    with pytest.raises(ValueError, match="snapshot fields"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_a_version_three_event_must_state_its_label_time_provenance(
    tmp_path: Path,
) -> None:
    """A current row that omits the marker does not masquerade as proof."""
    document = diagnostics_document()
    events_of(document)[0].pop("label_observed_verified")

    with pytest.raises(ValueError, match="snapshot fields"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_a_label_time_marker_that_is_not_a_boolean_is_refused(
    tmp_path: Path,
) -> None:
    """A marker outside true/false cannot be read as either."""
    document = diagnostics_document()
    events_of(document)[0]["label_observed_verified"] = "true"

    with pytest.raises(TypeError, match="must be a boolean"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_an_impossible_cacheable_intersection_is_refused(tmp_path: Path) -> None:
    """A labelled and cacheable pair must share at least one outcome row."""
    document = diagnostics_document()
    store = store_block(document)
    store["cacheable_labels"] = 0

    with pytest.raises(ValueError, match="possible intersection"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_an_irreconcilable_diagnostics_block_is_refused(tmp_path: Path) -> None:
    """A count that disagrees with its breakdown cannot become evidence."""
    document = diagnostics_document()
    store = store_block(document)
    store["outcome_records"] = 7

    with pytest.raises(ValueError, match="do not reconcile"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_a_dropped_event_is_refused(tmp_path: Path) -> None:
    """The event stream must hold one row per retained outcome."""
    document = diagnostics_document()
    events_of(document).clear()

    with pytest.raises(ValueError, match="events but its diagnostics"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_a_swapped_label_is_refused(tmp_path: Path) -> None:
    """Matching label totals cannot hide contradictory hit and miss counts."""
    document = diagnostics_document()
    events_of(document)[0]["label"] = "miss"

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
            history_from_records(adopted),
            now=NOW,
            history_loss=ACTIVE_FILE_ONLY,
        )
    )
    events_of(document)[0]["admitted_at"] = None

    with pytest.raises(ValueError, match="admission times on its events"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_diagnostics_labels_must_describe_theevents_of(tmp_path: Path) -> None:
    """A label count that disagrees with the events names no single history.

    The block stays internally consistent — one outcome row, no labels — so
    only the join to the event stream can refuse it.
    """
    document = diagnostics_document()
    store = store_block(document)
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
            history_from_records(first, second),
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
    git_event = next(event for event in events_of(document) if event["origin"] == "git")
    git_event["origin"] = "registry"

    with pytest.raises(ValueError, match="records its events by origin"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_diagnostics_must_describe_the_source_store(tmp_path: Path) -> None:
    """The two row totals in one snapshot cannot name different stores."""
    document = diagnostics_document()
    source = typ.cast("dict[str, object]", document["source"])
    source["records_total"] = 2
    source["outcome_records"] = 2

    with pytest.raises(ValueError, match="disagree with its source"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_an_unavailable_inventory_survives_the_snapshot(tmp_path: Path) -> None:
    """A failed inventory scan is distinct from a measured active-only read."""
    snapshot = build_snapshot(
        history_from_records(attributed_record(outcome="hit")),
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
    ("field", "value", "reason"),
    [
        ("attempt", "a" * 16, "must be stated together"),
        ("supervisor_generation", "generation-a", "must be stated together"),
        ("generation_digest", "abc", "generation_digest is outside its bounded shape"),
        ("record_evidence", "z" * 64, "record_evidence is outside its bounded shape"),
    ],
)
def test_a_version_seven_document_with_broken_correlation_is_refused(
    tmp_path: Path, field: str, value: object, reason: str
) -> None:
    """A version 7 field that cannot be trusted fails the load, not silently.

    Each case is a document a plausible implementation could write by
    omission: half a correlation pair, a truncated digest, or a digest in the
    wrong alphabet. The loader must refuse each one rather than accepting the
    row with the field dropped, because a dropped pair would let the row evade
    the duplicate and conflict gates while a truncated digest would compare as
    if it were complete evidence.
    """
    document = diagnostics_document()
    rows = typ.cast("list[dict[str, object]]", document["events"])
    rows[0][field] = value

    with pytest.raises(ValueError, match=reason):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


@pytest.mark.parametrize("attempt", ["a" * 33, "bad/token", ""])
def test_a_version_seven_document_with_a_bad_attempt_is_refused(
    tmp_path: Path, attempt: str
) -> None:
    """The attempt half of the pair is validated on its own.

    Each document keeps a valid `supervisor_generation`, so a loader that
    checked only the generation would accept the row and admit a token the
    recorder can never write.
    """
    document = diagnostics_document()
    rows = typ.cast("list[dict[str, object]]", document["events"])
    rows[0]["supervisor_generation"] = "a" * 64
    rows[0]["attempt"] = attempt

    with pytest.raises(ValueError, match="correlation token"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_the_longest_accepted_token_pair_round_trips(tmp_path: Path) -> None:
    """Both snapshot bounds are inclusive, so the longest pair loads."""
    document = diagnostics_document()
    rows = typ.cast("list[dict[str, object]]", document["events"])
    rows[0]["supervisor_generation"] = "b" * 64
    rows[0]["attempt"] = "a" * 32
    path = write_json(tmp_path / "history.jsonl", document)

    loaded = load_snapshot(path)

    assert loaded.events[0].attempt == "a" * 32, "the attempt survives its bound"
    assert loaded.events[0].supervisor_generation == "b" * 64, (
        "and so does the generation"
    )
