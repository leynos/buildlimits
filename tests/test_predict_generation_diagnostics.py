"""The two per-generation label-loss maps and their reconciliation rules.

A snapshot states label loss per sccache generation twice: once for every
outcome row and once for the labelled rows within those buckets. The two maps
partition different totals but must name the same generations, so the checks
that keep them honest differ from the ones every other breakdown uses. They
live here so the document-shape contracts stay in one module and this one
states what the dimension itself must satisfy.
"""

import dataclasses as dc
import typing as typ
from pathlib import Path

import pytest
from outcome_test_helpers import attributed_record
from predict_diagnostics_helpers import (
    NOW,
    diagnostics_document,
    document_as_version,
    history_from_records,
    store_block,
)
from predict_test_helpers import ACTIVE_FILE_ONLY, write_json

from buildlimits.predict.dataset import StoreHistory, build_snapshot
from buildlimits.predict.diagnostics_generations import (
    GENERATION_VERSION,
    require_generation_labels,
    unrecorded_generations,
)
from buildlimits.predict.snapshot_codec import SNAPSHOT_VERSION, snapshot_to_dict
from buildlimits.predict.snapshot_store import load_snapshot

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import OutcomeRecord


@pytest.mark.parametrize(
    ("rows", "labelled", "declared", "expected"),
    [
        pytest.param(
            {"a": 1},
            {"b": 1},
            1,
            "name unknown buckets",
            id="unknown-bucket",
        ),
        pytest.param(
            {"a": 1},
            {"a": 2},
            2,
            "exceed their own generation totals",
            id="labels-exceed-rows",
        ),
        pytest.param(
            {"a": 1, "unrecorded": 1},
            {"a": 1},
            2,
            "do not sum to the labelled count",
            id="labels-do-not-sum",
        ),
    ],
)
def test_a_generation_label_map_that_cannot_describe_a_history_is_refused(
    rows: dict[str, int],
    labelled: dict[str, int],
    declared: int,
    expected: str,
) -> None:
    """Each way the labelled map can contradict its document is refused.

    The labelled map partitions the labelled rows while the row map partitions
    all of them, so a document that breaks either relationship states two
    histories at once. A reader that printed it would report counts no single
    store ever held. ``declared`` is the document's own labelled total, which
    the map must equal; the labelling cases set it to two so the sum rule is
    the one that fires.
    """
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


def test_the_generation_version_matches_the_snapshot_version() -> None:
    """The two constants name one format, and must not drift apart.

    `diagnostics_generations` repeats the version rather than importing it, to
    avoid the cycle the module docstring records. A repeated constant can drift
    silently, so this pins the pair: if the snapshot version moves, the
    generation rule moves with it.
    """
    assert GENERATION_VERSION == SNAPSHOT_VERSION, (
        "the generation maps arrive with the snapshot version that defines them"
    )


def _overflowing_history() -> StoreHistory:
    """Return a history of 70 generations whose row and label rankings differ.

    The first 63 generations carry two rows each and no labels, so they fill
    the retained row keys exactly. Generations 63 to 69 carry one row each and
    all seven labels, so a label map ranked on its own would keep keys the row
    map had dropped. That is the arrangement the shared key set has to survive.

    Returns
    -------
    StoreHistory
        The decoded history, ready to snapshot.
    """
    records: list[OutcomeRecord] = []
    for index in range(70):
        digest = f"{index:064x}"
        labelled = index >= 63
        for _ in range(1 if labelled else 2):
            records.append(
                dc.replace(
                    attributed_record(outcome="hit" if labelled else "unattributed"),
                    generation_digest=digest,
                    outcome_reason="none" if labelled else "mixed_window",
                )
            )
    return history_from_records(*records)


def test_generation_maps_sharing_an_overflow_keep_one_key_set() -> None:
    """More than 64 generations keep both maps on the same retained keys.

    Each map used to be bounded on its own, so a generation that carried many
    labels but few rows could survive in the labelled map after the row map had
    dropped it. The loader then refused the document for naming a bucket the
    row map could not explain. Both maps must now share one retained set, with
    everything else merged into ``other`` on both sides.
    """
    accounting = _overflowing_history().accounting

    assert set(accounting.labels_by_generation) <= set(accounting.by_generation), (
        "the labelled map names no bucket the row map lacks"
    )
    assert len(accounting.by_generation) <= 64, "the row map stays bounded"
    assert sum(accounting.by_generation.values()) == accounting.outcome_records, (
        "the row map still sums to every outcome row"
    )
    assert sum(accounting.labels_by_generation.values()) == accounting.labels, (
        "the labelled map still sums to every label"
    )
    assert accounting.by_generation["other"] > 0, "the overflow is absorbed"
    assert accounting.labels_by_generation["other"] > 0, (
        "the label overflow is absorbed on the same bucket"
    )


def test_an_overflowing_history_round_trips_through_a_snapshot(
    tmp_path: Path,
) -> None:
    """A snapshot of more than 64 generations loads without a refusal.

    The paired maps are only useful if the document that carries them can be
    read back, so this drives the real writer and loader rather than the
    accounting value alone.
    """
    snapshot = build_snapshot(
        _overflowing_history(), now=NOW, history_loss=ACTIVE_FILE_ONLY
    )
    path = write_json(tmp_path / "history.jsonl", snapshot_to_dict(snapshot))

    written = snapshot.diagnostics
    assert written is not None, "the fixture wrote a block"
    loaded = load_snapshot(path)

    assert loaded.diagnostics is not None, "the block survives the round trip"
    store = loaded.diagnostics.store
    assert store.by_generation == written.store.by_generation, (
        "the row map is restored unchanged"
    )
    assert store.labels_by_generation == written.store.labels_by_generation, (
        "the labelled map is restored unchanged"
    )
    assert set(store.labels_by_generation) <= set(store.by_generation), (
        "the reloaded maps still share one key set"
    )


@pytest.mark.parametrize("present", ["by_generation", "labels_by_generation"])
def test_a_document_stating_half_the_generation_pair_is_refused(
    tmp_path: Path, present: str
) -> None:
    """One generation map without the other claims a dimension it cannot have.

    The pair is written together and partitions the same buckets, so a document
    holding exactly one is not a shape any release produced. It must be refused
    rather than completed by synthesis, which would invent the half it lacks.
    """
    document = document_as_version(diagnostics_document(), 3)
    store = store_block(document)
    store.pop("by_generation", None)
    store.pop("labels_by_generation", None)
    store[present] = {"unrecorded": 1}

    with pytest.raises(TypeError, match="states a generation map without"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_a_document_stating_neither_generation_map_takes_the_absent_branch(
    tmp_path: Path,
) -> None:
    """A version 3 history with neither map is unrecorded, not corrupt.

    This is the case that must keep working: every released version 1 to 3
    document looks like this, and refusing it would make old snapshots
    unreadable.
    """
    document = document_as_version(diagnostics_document(), 3)

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert loaded.diagnostics is not None, "the block still loads"
    assert loaded.diagnostics.store.by_generation == {"unrecorded": 1}, (
        "the row is stated as unattributed"
    )
    assert loaded.diagnostics.store.labels_by_generation == {"unrecorded": 1}, (
        "the label is stated as unattributed too"
    )
