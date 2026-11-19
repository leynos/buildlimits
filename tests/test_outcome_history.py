"""Archive-aware history reading and its command-line surface.

History spans archived XZ segments, sealed plain segments and the writer's
active file. These tests build a real segmented store with the writer, archive
it with the archiver, and then read it back through the history view, the replay
command and the dataset command.
"""

import datetime as dt
import lzma
import typing as typ
from pathlib import Path

import pytest
from outcome_store_helpers import wait_for_results, wait_for_salt
from outcome_test_helpers import attributed_record

from buildlimits import outcome_history, outcome_replay_cli
from buildlimits.outcome_archive import RunOptions, run_archive
from buildlimits.outcome_record_format import (
    DecodedStore,
    OutcomeRecord,
    StoreRecord,
    WindowSummary,
)
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segments import SegmentLayout
from buildlimits.outcome_store import StoreWriter
from buildlimits.outcome_store_io import read_store
from buildlimits.predict import dataset_cli
from buildlimits.predict.snapshot_store import load_snapshot

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
_SALT_NAME = "cache-outcomes.salt"


def _policy(segment_bytes: int) -> RetentionPolicy:
    """Build an unlimited-budget policy that seals at ``segment_bytes``.

    Returns
    -------
    RetentionPolicy
        A policy with no age or byte eviction, so archived history survives.
    """
    return RetentionPolicy(
        max_age=dt.timedelta(days=30),
        max_bytes=0,
        max_records=None,
        headroom_bytes=max(segment_bytes, 1) * 4,
        segment_bytes=segment_bytes,
    )


def _ready_writer(path: Path, policy: RetentionPolicy) -> StoreWriter:
    """Start a writer and wait until it accepts appends.

    Returns
    -------
    StoreWriter
        The initialized writer.
    """
    writer = StoreWriter(path, policy=policy, wall_clock=lambda: NOW)
    try:
        wait_for_salt(writer)
        wait_for_results(writer, operation="load")
    except BaseException:
        writer.close(1.0)
        raise
    return writer


def _records(first: int, count: int) -> tuple[OutcomeRecord, ...]:
    """Build ``count`` attributed records observed one minute apart.

    Returns
    -------
    tuple[OutcomeRecord, ...]
        The records, oldest observation first.
    """
    return tuple(
        attributed_record(observed_at=NOW - dt.timedelta(minutes=index))
        for index in range(first, first + count)
    )


def _window(first: int, count: int) -> WindowSummary:
    """Build one window summary carrying ``count`` requests.

    Returns
    -------
    WindowSummary
        The summary, so a test can recognize it among outcome records.
    """
    return WindowSummary(
        observed_at=NOW - dt.timedelta(minutes=first),
        server_version="1.0.0",
        window_groups=1,
        requests_not_compile=count,
        requests_not_cacheable=0,
        predicted_not_compile=0,
        predicted_declined=0,
        predicted_cacheable=0,
    )


def _write(
    path: Path,
    policy: RetentionPolicy,
    records: tuple[StoreRecord, ...],
) -> None:
    """Append ``records`` in order and close the writer.

    The writer must have settled before the first append, because a record
    offered during startup is discarded as ``save_failed``. Closing then drains
    the queue, so every record is on disk before this returns.
    """
    writer = _ready_writer(path, policy)
    try:
        for record in records:
            writer.append(record)
    finally:
        writer.close(2.0)


def _archived_segments(path: Path, count: int) -> None:
    """Seal ``count`` one-record segments and archive them."""
    policy = _policy(1)
    _write(path, policy, _records(0, count))
    report = run_archive(SegmentLayout.beside(path), policy, RunOptions(now=NOW))
    assert report.status == "ok", "the archiver publishes without fault"
    assert [action.action for action in report.actions] == ["archived"] * count, (
        "every sealed source is archived"
    )


def _active_records(path: Path, first: int, count: int) -> None:
    """Append records that stay in the active file."""
    _write(path, _policy(10**6), _records(first, count))


def _observed(records: tuple[StoreRecord, ...]) -> tuple[dt.datetime, ...]:
    """Return the observation times of decoded records in order.

    Returns
    -------
    tuple[datetime, ...]
        Each record's ``observed_at``.
    """
    return tuple(record.observed_at for record in records)


def test_history_reads_archived_and_active_records_in_append_order(
    tmp_path: Path,
) -> None:
    """Archived rows and active rows share one append-ordered history."""
    store = tmp_path / "cache-outcomes.jsonl"
    _archived_segments(store, 3)
    _active_records(store, first=3, count=2)

    active_only = typ.cast("DecodedStore", read_store(store))
    history = outcome_history.read_history_beside(store)

    assert len(active_only.records) == 2, (
        "the active file alone holds only the newest rows"
    )
    assert _observed(history.records) == tuple(
        NOW - dt.timedelta(minutes=index) for index in range(5)
    ), "history reads archived rows before active rows without a gap"


def test_history_reads_a_sealed_source_before_its_archive(
    tmp_path: Path,
) -> None:
    """A sealed source that survived a crash is read instead of its archive."""
    store = tmp_path / "cache-outcomes.jsonl"
    policy = _policy(1)
    _write(store, policy, _records(0, 2))
    layout = SegmentLayout.beside(store)
    run_archive(layout, policy, RunOptions(now=NOW))
    archive = next(layout.archive_dir.glob("*.jsonl.xz"))
    source = layout.sealed_dir / archive.name.removesuffix(".xz")
    source.write_bytes(lzma.decompress(archive.read_bytes()))
    source.chmod(0o600)

    history = outcome_history.read_history_beside(store)

    assert len(history.records) == 2, (
        "reading the surviving sealed source does not duplicate its archived copy"
    )


def test_history_defaults_to_the_newest_salt_domain(tmp_path: Path) -> None:
    """A salt change starts a new domain; the default reads that domain."""
    store = tmp_path / "cache-outcomes.jsonl"
    policy = _policy(1)
    _write(store, policy, (_window(0, 1), _window(1, 1)))
    (tmp_path / _SALT_NAME).unlink()
    _active_records(store, first=2, count=2)
    newest_salt = typ.cast("DecodedStore", read_store(store)).header.salt_id

    default = outcome_history.read_history_beside(store)
    selected = outcome_history.read_history_beside(
        store, outcome_history.HistorySelection(salt_id=newest_salt)
    )
    every = outcome_history.read_history_beside(
        store, outcome_history.HistorySelection(all_salts=True)
    )

    assert len(default.records) == 2, "the newest domain is the default"
    assert default.header.salt_id == newest_salt, "the newest header is reported"
    assert _observed(default.records) == _observed(selected.records), (
        "an explicit newest-salt selection matches the default"
    )
    assert len(every.records) == 4, "every domain is read in append order"


def test_history_reports_an_unknown_salt(tmp_path: Path) -> None:
    """A salt no retained segment carries is a closed fault."""
    store = tmp_path / "cache-outcomes.jsonl"
    _archived_segments(store, 1)

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        outcome_history.read_history_beside(
            store, outcome_history.HistorySelection(salt_id="0" * 64)
        )

    assert raised.value.reason == "unknown_salt", "the reason is closed"


def test_history_reports_a_corrupt_archive(tmp_path: Path) -> None:
    """A damaged archive is a closed fault, not a shortened history."""
    store = tmp_path / "cache-outcomes.jsonl"
    _archived_segments(store, 1)
    archive = next((tmp_path / "cache-outcomes.archive").glob("*.jsonl.xz"))
    archive.write_bytes(b"\x00" * 16)

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        outcome_history.read_history_beside(store)

    assert raised.value.reason in {"bad_xz", "truncated_xz"}, "the fault is bounded"


def test_history_reports_a_missing_store(tmp_path: Path) -> None:
    """A store with no active file and no segments has no history."""
    with pytest.raises(FileNotFoundError):
        outcome_history.read_history_beside(tmp_path / "absent.jsonl")


def test_replay_reads_archived_history_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--history`` lets the replay command see archived rows."""
    store = tmp_path / "cache-outcomes.jsonl"
    _archived_segments(store, 2)
    _active_records(store, first=2, count=1)

    status = outcome_replay_cli.main([
        "h2",
        "--store",
        str(store),
        "--history",
        "--json",
    ])
    output = capsys.readouterr().out

    assert status == 0, "a readable history produces a report"
    assert '"attributed_outcomes": 3' in output, "archived rows are replayed"


def test_replay_rejects_an_unknown_salt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A salt the store never carried exits one with a bounded reason."""
    store = tmp_path / "cache-outcomes.jsonl"
    _archived_segments(store, 1)

    status = outcome_replay_cli.main([
        "h2",
        "--store",
        str(store),
        "--salt-id",
        "0" * 64,
    ])

    assert status == 1, "an unknown domain is a command failure"
    assert "unknown_salt" in capsys.readouterr().err, "the reason is reported"


def test_replay_refuses_two_domain_selectors(tmp_path: Path) -> None:
    """``--salt-id`` and ``--all-salts`` cannot be combined."""
    status = outcome_replay_cli.main([
        "h2",
        "--store",
        str(tmp_path / "store.jsonl"),
        "--salt-id",
        "abc",
        "--all-salts",
    ])

    assert status == 2, "conflicting selectors are a usage failure"


def test_dataset_reads_archived_history_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--history`` lets the snapshot command include archived rows."""
    store = tmp_path / "cache-outcomes.jsonl"
    snapshot = tmp_path / "history.jsonl"
    _archived_segments(store, 2)
    _active_records(store, first=2, count=1)

    status = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(snapshot),
        "--skip-journal",
        "--history",
    ])

    assert status == 0, "the snapshot is written"
    assert "events=3" in capsys.readouterr().out, "archived rows become events"
    assert len(load_snapshot(snapshot).events) == 3, "the snapshot holds them"


def test_dataset_reports_a_rejected_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupt archive is a bounded failure, never a partial snapshot."""
    store = tmp_path / "cache-outcomes.jsonl"
    snapshot = tmp_path / "history.jsonl"
    _archived_segments(store, 1)
    archive = next((tmp_path / "cache-outcomes.archive").glob("*.jsonl.xz"))
    archive.write_bytes(b"\x00" * 16)

    status = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(snapshot),
        "--skip-journal",
        "--history",
    ])

    assert status == 1, "a rejected history is fatal"
    assert "rejected as" in capsys.readouterr().err, "the reason is reported"
    assert not snapshot.exists(), "nothing is written behind a fault"
