"""The store-to-snapshot assertions for the real-sccache observer check.

The check's extraction step reads the retained rows back through the strict
codec and the snapshot builder. Keeping it apart from the recorder driving
keeps each module to one responsibility, and lets a reader see exactly what
the controlled run proves about the persisted evidence.
"""

import datetime as dt
import typing as typ

import pytest
from sccache_outcome_observer_support import outcomes

from buildlimits import outcome_store_io
from buildlimits.outcome_record_codec import decode_lines
from buildlimits.outcome_record_format import (
    DecodedStore,
    StoreHeader,
)
from buildlimits.predict.accounting import HistoryLoss
from buildlimits.predict.config_loader import load_config
from buildlimits.predict.dataset import Snapshot, build_snapshot
from buildlimits.predict.records import decode_history
from buildlimits.predict.snapshot_store import load_snapshot, save_snapshot

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_key import Salt
    from buildlimits.outcome_record_format import StoreRecord


def round_trip(records: list[StoreRecord], tmp_path: Path, salt_id: str) -> Snapshot:
    """Write rows to the store, re-read them, and save and load a snapshot.

    The rows are written through the same writer the supervisor uses and read
    back from the file, then the snapshot is saved and loaded the same way, so
    a check covers the bytes an operator would keep rather than an in-memory
    document.

    Returns
    -------
    Snapshot
        The history the store's own reader and the snapshot loader accept.
    """
    header = StoreHeader(salt_id, dt.datetime.now(dt.UTC))
    store = tmp_path / "cache-outcomes.jsonl"
    outcome_store_io.replace_store(store, header, typ.cast("typ.Any", records))
    decoded = decode_lines(store.read_text(encoding="utf-8"))
    if not isinstance(decoded, DecodedStore):
        pytest.fail(f"the retained rows did not decode: {decoded.reason}")
    snapshot = build_snapshot(
        decode_history(decoded),
        now=header.written_at,
        history_loss=HistoryLoss.active_file_only(),
    )
    path = tmp_path / "history.jsonl"
    save_snapshot(path, snapshot)
    return load_snapshot(path)


def assert_extraction(records: list[StoreRecord], salt: Salt, tmp_path: Path) -> None:
    """Prove the measured rows survive the store files and the snapshot files."""
    snapshot = round_trip(records, tmp_path, salt.salt_id)
    stats = snapshot.stats()
    outcome_rows = len(outcomes(tuple(records)))

    assert stats.events == outcome_rows, (
        "every recorded outcome row reaches the snapshot, declined rows included"
    )
    assert stats.labelled == 2, "the controlled miss and hit are the labels"
    assert stats.admission_recorded == stats.events, (
        "every retained outcome carries its admission time"
    )
    assert stats.admission_usable == stats.events, (
        "each admission precedes its own label"
    )
    assert stats.unverified_label_time == 0, (
        "every retained row proves the poll boundary that accepted it"
    )
    assert stats.scored == 2, "only the controlled miss and hit are scorable"
    unscorable = sum(
        1
        for record in outcomes(tuple(records))
        if record.outcome not in {"hit", "miss"}
    )
    assert stats.excluded == {"unknown_outcome": unscorable}, (
        "every excluded row names its closed reason, and no other gate rejected "
        "a row that was verified at the poll boundary"
    )
    assert all(
        record.label_time_provenance == "verified"
        for record in outcomes(tuple(records))
    ), "every real row proves the moment its label became known"
    assert all(
        record.generation_digest is not None
        for record in outcomes(tuple(records))
        if record.outcome in {"hit", "miss"}
    ), "each labelled row names the sccache generation its reading observed"
    assert snapshot.diagnostics is not None, "the accounting block is present"
    assert snapshot.diagnostics.store.by_result["hit"] == 1, "one real hit"
    assert snapshot.diagnostics.store.by_result["miss"] == 1, "one real miss"
    assert snapshot.diagnostics.store.by_reason["mixed_window"] == 2, (
        "both mixed rows name their refusal"
    )
    assert snapshot.diagnostics.store.by_reason["timeout"] == 1, "the timeout is named"
    _assert_replay_trace(snapshot, records)


def _assert_replay_trace(snapshot: object, records: list[StoreRecord]) -> None:
    """Trace each invocation from admission through replay and provenance.

    Parameters
    ----------
    snapshot : object
        The snapshot the controlled rows were extracted into.
    records : list[StoreRecord]
        The retained rows, in the order the recorder produced them.
    """
    from buildlimits.predict.events import label_moment
    from buildlimits.predict.harness import walk_forward

    typed = typ.cast("Snapshot", snapshot)
    walk = walk_forward(typed.events, load_config())
    verified = [event for event in typed.events if event.label_observed_verified]

    assert len(verified) == len(outcomes(tuple(records))), (
        "every retained invocation survives extraction with its availability"
    )
    assert all(event.attempt is not None for event in verified), (
        "each extracted event still names its attempt"
    )
    assert all(event.supervisor_generation is not None for event in verified), (
        "and the generation that stamped it"
    )
    assert all(
        event.admitted_at is not None
        and event.admitted_at <= typ.cast("dt.datetime", label_moment(event))
        for event in verified
    ), "each invocation's decision precedes the moment its label was accepted"
    assert sum(len(group) for group in walk.moments) == len(verified), (
        "the replay answers every invocation the extraction verified"
    )
    assert typed.source.salt_id, "the snapshot states the store domain it came from"
    assert typed.source.written_at is not None, "and when the store was written"
