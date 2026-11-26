"""The store-to-snapshot assertions for the real-sccache observer check.

The check's extraction step reads the retained rows back through the strict
codec and the snapshot builder. Keeping it apart from the recorder driving
keeps each module to one responsibility, and lets a reader see exactly what
the controlled run proves about the persisted evidence.
"""

import datetime as dt
import typing as typ

import pytest
from rustc_query_host_checks import require
from sccache_outcome_observer_support import outcomes

from buildlimits import outcome_store_io
from buildlimits.outcome_record_codec import decode_lines
from buildlimits.outcome_record_format import (
    DecodedStore,
    StoreHeader,
)
from buildlimits.predict.accounting import Diagnostics, HistoryLoss
from buildlimits.predict.config_loader import load_config
from buildlimits.predict.dataset import Snapshot, build_snapshot
from buildlimits.predict.records import decode_history
from buildlimits.predict.snapshot_store import load_snapshot, save_snapshot

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_key import Salt
    from buildlimits.outcome_record_format import OutcomeRecord, StoreRecord


def decoded_store(document: str, what: str) -> DecodedStore:
    """Decode one store document or fail the check with its closed reason.

    Returns
    -------
    DecodedStore
        The validated header and records in append order.
    """
    decoded = decode_lines(document)
    if not isinstance(decoded, DecodedStore):
        pytest.fail(f"the {what} rows did not decode: {decoded.reason}")
    return decoded


def snapshot_of(decoded: DecodedStore, tmp_path: Path, name: str) -> Snapshot:
    """Build a snapshot from decoded rows and save and load it as a file.

    Returns
    -------
    Snapshot
        The history the snapshot loader accepts.
    """
    snapshot = build_snapshot(
        decode_history(decoded),
        now=decoded.header.written_at,
        history_loss=HistoryLoss.active_file_only(),
    )
    path = tmp_path / f"{name}-history.jsonl"
    save_snapshot(path, snapshot)
    return load_snapshot(path)


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
    decoded = decoded_store(store.read_text(encoding="utf-8"), "retained")
    return snapshot_of(decoded, tmp_path, "history")


def assert_extraction(records: list[StoreRecord], salt: Salt, tmp_path: Path) -> None:
    """Prove the measured rows survive the store files and the snapshot files."""
    snapshot = round_trip(records, tmp_path, salt.salt_id)
    stats = snapshot.stats()
    rows = outcomes(tuple(records))
    unscorable = sum(1 for record in rows if record.outcome not in {"hit", "miss"})
    diagnostics = snapshot.diagnostics

    require(
        condition=stats.events == len(rows),
        message="every outcome row reaches the snapshot, declined rows included",
    )
    require(
        condition=stats.labelled == 2,
        message="the controlled miss and hit are the labels",
    )
    require(
        condition=stats.admission_recorded == stats.events,
        message="every retained outcome carries its admission time",
    )
    require(
        condition=stats.admission_usable == stats.events,
        message="each admission precedes its own label",
    )
    require(
        condition=stats.unverified_label_time == 0,
        message="every retained row proves the poll boundary that accepted it",
    )
    require(
        condition=stats.scored == 2,
        message="only the controlled miss and hit are scorable",
    )
    require(
        condition=stats.excluded == {"unknown_outcome": unscorable},
        message=(
            "every excluded row names its closed reason, and no other gate "
            "rejected a row verified at the poll boundary"
        ),
    )
    require(
        condition=all(r.label_time_provenance == "verified" for r in rows),
        message="every real row proves the moment its label became known",
    )
    require(
        condition=all(
            r.generation_digest is not None
            for r in rows
            if r.outcome in {"hit", "miss"}
        ),
        message="each labelled row names the generation its reading observed",
    )
    require(
        condition=diagnostics is not None,
        message="the accounting block is present",
    )
    accounting = typ.cast("Diagnostics", diagnostics)
    require(condition=accounting.store.by_result["hit"] == 1, message="one real hit")
    require(condition=accounting.store.by_result["miss"] == 1, message="one real miss")
    require(
        condition=accounting.store.by_reason["mixed_window"] == 2,
        message="both mixed rows name their refusal",
    )
    require(
        condition=accounting.store.by_reason["timeout"] == 1,
        message="the timeout is named",
    )
    _assert_replay_trace(snapshot, rows)


def _assert_replay_trace(snapshot: object, rows: tuple[OutcomeRecord, ...]) -> None:
    """Trace each invocation from admission through replay and provenance.

    Parameters
    ----------
    snapshot : object
        The snapshot the controlled rows were extracted into.
    rows : tuple[OutcomeRecord, ...]
        The outcome rows, in the order the recorder produced them.
    """
    from buildlimits.predict.events import label_moment
    from buildlimits.predict.harness import walk_forward

    typed = typ.cast("Snapshot", snapshot)
    walk = walk_forward(typed.events, load_config())
    verified = [event for event in typed.events if event.label_observed_verified]

    require(
        condition=len(verified) == len(rows),
        message="every retained invocation keeps its availability",
    )
    require(
        condition=all(event.attempt is not None for event in verified),
        message="each extracted event still names its attempt",
    )
    require(
        condition=all(event.supervisor_generation is not None for event in verified),
        message="and the generation that stamped it",
    )
    require(
        condition=all(
            event.admitted_at is not None
            and event.admitted_at <= typ.cast("dt.datetime", label_moment(event))
            for event in verified
        ),
        message="each decision precedes the moment its label was accepted",
    )
    require(
        condition=sum(len(group) for group in walk.moments) == len(verified),
        message="the replay answers every invocation the extraction verified",
    )
    require(
        condition=bool(typed.source.salt_id),
        message="the snapshot states the store domain it came from",
    )
    require(
        condition=typed.source.written_at is not None,
        message="and when the store was written",
    )
