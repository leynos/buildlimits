"""End-to-end archive runs against a real writer-produced store.

The writer seals closed history; the archiver then plans, compresses, verifies,
retains and reports in one timer firing. These tests build real segments with
the writer, run the archiver over them, and assert the observable contract:
closed exit statuses, one-pass retention, no clobbering, and a history view
that is identical before and after archiving.
"""

import datetime as dt
import json
import lzma
import os
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from outcome_store_helpers import wait_for_results, wait_for_salt
from outcome_test_helpers import attributed_record

from buildlimits import outcome_history, outcome_record_encode
from buildlimits.outcome_archive import (
    EXIT_CORRUPT,
    EXIT_OK,
    EXIT_TEMPFAIL,
    RunOptions,
    RunReport,
    inspect_store,
    run_archive,
)
from buildlimits.outcome_archive_lock import ArchiveLock
from buildlimits.outcome_record_format import DecodedStore, StoreHeader, StoreRecord
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segments import (
    SegmentLayout,
    archive_name,
    sidecar_name,
)
from buildlimits.outcome_store import StoreWriter

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)


def _policy(
    segment_bytes: int = 1,
    *,
    max_age_days: int = 30,
    max_bytes: int = 0,
    headroom_bytes: int | None = None,
) -> RetentionPolicy:
    """Build one archiver policy with the bounds a test needs.

    Returns
    -------
    RetentionPolicy
        A policy that seals one record per segment by default.
    """
    return RetentionPolicy(
        max_age=dt.timedelta(days=max_age_days),
        max_bytes=max_bytes,
        max_records=None,
        headroom_bytes=(
            max(segment_bytes, 1) * 4 if headroom_bytes is None else headroom_bytes
        ),
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


def _records(observed: dt.datetime, count: int) -> tuple[StoreRecord, ...]:
    """Build ``count`` records one minute apart from ``observed``.

    Returns
    -------
    tuple[StoreRecord, ...]
        The records, oldest first.
    """
    return tuple(
        attributed_record(observed_at=observed + dt.timedelta(minutes=index))
        for index in range(count)
    )


def _write(
    path: Path, records: tuple[StoreRecord, ...], policy: RetentionPolicy
) -> None:
    """Append records and close the writer, draining every append."""
    writer = _ready_writer(path, policy)
    try:
        for record in records:
            writer.append(record)
    finally:
        writer.close(2.0)


def _layout(tmp_path: Path) -> SegmentLayout:
    """Return the layout for a store below ``tmp_path``.

    Returns
    -------
    SegmentLayout
        The store's segment layout.
    """
    return SegmentLayout.beside(tmp_path / "cache-outcomes.jsonl")


def _run(
    tmp_path: Path,
    policy: RetentionPolicy,
    *,
    dry_run: bool = False,
) -> RunReport:
    """Run the archiver once at a fixed wall time.

    Returns
    -------
    RunReport
        The run report.
    """
    return run_archive(_layout(tmp_path), policy, RunOptions(now=NOW, dry_run=dry_run))


def _archives(tmp_path: Path) -> list[Path]:
    """List the published archives in sequence order.

    Returns
    -------
    list[Path]
        The ``.jsonl.xz`` files.
    """
    directory = _layout(tmp_path).archive_dir
    return sorted(directory.glob("*.jsonl.xz")) if directory.exists() else []


def _document(records: tuple[StoreRecord, ...], salt_id: str = "salt-1") -> bytes:
    """Encode a header and records as one JSON Lines document.

    Returns
    -------
    bytes
        The complete document.
    """
    lines = [outcome_record_encode.encode_line(StoreHeader(salt_id, NOW))]
    lines.extend(outcome_record_encode.encode_line(record) for record in records)
    return ("\n".join(lines) + "\n").encode()


def _observed(history: DecodedStore) -> tuple[dt.datetime, ...]:
    """Return the observation times of decoded history records.

    Returns
    -------
    tuple[datetime, ...]
        Each record's ``observed_at``.
    """
    return tuple(record.observed_at for record in history.records)


def test_an_empty_store_records_a_successful_run(tmp_path: Path) -> None:
    """A timer firing on a store with no history still writes its state."""
    report = _run(tmp_path, _policy())

    layout = _layout(tmp_path)
    assert report.status == "ok", "an empty run is successful"
    assert report.exit_status == EXIT_OK, "the timer sees success"
    assert not report.actions, "there is nothing to do"
    assert report.retained_bytes == 0, "no history is retained"
    assert not report.gaps, "an empty store has no gaps"
    retention = json.loads(layout.retention_file.read_text(encoding="utf-8"))
    last_run = json.loads(layout.last_run_file.read_text(encoding="utf-8"))
    assert retention["floor_sequence"] == 0, "the floor records no history"
    assert retention["schema"] == 1, "the state schema is closed"
    assert last_run["status"] == "ok", "the last run is the same report"
    assert last_run["schema"] == 1, "the report schema is closed"


def test_a_dry_run_changes_nothing(tmp_path: Path) -> None:
    """A plan may be inspected without taking the lock or writing a file."""
    store = tmp_path / "cache-outcomes.jsonl"
    _write(store, _records(NOW, 2), _policy())

    report = _run(tmp_path, _policy(), dry_run=True)

    layout = _layout(tmp_path)
    assert report.dry_run, "the report says no file changed"
    assert report.plan is not None, "a dry run reports the plan it would run"
    assert report.plan.archive == 2, "the plan counts both sealed sources"
    assert len(list(layout.sealed_dir.glob("*.jsonl"))) == 2, (
        "both sources remain sealed"
    )
    assert not layout.archive_dir.exists(), "no archive state is created"
    assert not layout.retention_file.exists(), "no retention state is written"


def test_inspect_is_a_dry_run(tmp_path: Path) -> None:
    """The inspect entry point never mutates the store."""
    store = tmp_path / "cache-outcomes.jsonl"
    _write(store, _records(NOW, 1), _policy())

    report = inspect_store(_layout(tmp_path), _policy())

    assert report.dry_run, "inspection is always a dry run"
    assert report.plan is not None, "inspection reports the plan it would run"
    assert report.plan.archive == 1, "the pending source is reported"
    assert not _archives(tmp_path), "a dry run publishes nothing"


def test_a_held_lock_reports_a_retryable_status(tmp_path: Path) -> None:
    """A timer firing during another run exits 75 instead of waiting."""
    store = tmp_path / "cache-outcomes.jsonl"
    _write(store, _records(NOW, 1), _policy())
    layout = _layout(tmp_path)

    with ArchiveLock(layout.lock_file):
        report = _run(tmp_path, _policy())

    assert report.status == "busy", "the run did not start"
    assert report.exit_status == EXIT_TEMPFAIL, "a retry may succeed"
    assert report.reason == "busy", "the reason is closed"
    assert not _archives(tmp_path), "a busy run publishes nothing"


def test_insufficient_headroom_reports_a_retryable_status(tmp_path: Path) -> None:
    """A full filesystem defers the run rather than failing it permanently."""
    store = tmp_path / "cache-outcomes.jsonl"
    _write(store, _records(NOW, 1), _policy())

    report = _run(tmp_path, _policy(headroom_bytes=1 << 62))

    assert report.status == "no_headroom", "the run did not start"
    assert report.exit_status == EXIT_TEMPFAIL, "the run is retryable"
    assert report.reason == "no_headroom", "the reason is closed"


def test_a_corrupt_source_is_quarantined_and_reported(tmp_path: Path) -> None:
    """A segment that cannot be decoded leaves the store and is named."""
    store = tmp_path / "cache-outcomes.jsonl"
    _write(store, _records(NOW, 1), _policy())
    layout = _layout(tmp_path)
    source = next(layout.sealed_dir.glob("*.jsonl"))
    source.write_bytes(b"not a header\n")

    report = _run(tmp_path, _policy())

    assert report.status == "corrupt", "the run reports corruption"
    assert report.exit_status == EXIT_CORRUPT, "an operator must act"
    assert report.failures == ((1, "corrupt"),), "the sequence and reason are named"
    assert [action.action for action in report.actions] == ["quarantined"], (
        "the source is moved aside"
    )
    assert not _archives(tmp_path), "no archive is published"
    assert list(layout.quarantine_dir.iterdir()), "the source is preserved"


def test_a_missing_sidecar_is_repaired(tmp_path: Path) -> None:
    """A sidecar lost to a crash is rebuilt from the verified archive."""
    store = tmp_path / "cache-outcomes.jsonl"
    _write(store, _records(NOW, 1), _policy())
    _run(tmp_path, _policy())
    layout = _layout(tmp_path)
    (layout.archive_dir / sidecar_name(1)).unlink()

    report = _run(tmp_path, _policy())

    assert report.status == "ok", "healing is not a failure"
    assert [action.action for action in report.actions] == ["repaired"], (
        "the sidecar is rebuilt"
    )
    assert (layout.archive_dir / sidecar_name(1)).exists(), "the sidecar is back"


def test_a_damaged_archive_cannot_be_healed(tmp_path: Path) -> None:
    """An archive that no longer verifies is quarantined, not described."""
    store = tmp_path / "cache-outcomes.jsonl"
    _write(store, _records(NOW, 1), _policy())
    _run(tmp_path, _policy())
    layout = _layout(tmp_path)
    archive = layout.archive_dir / archive_name(1)
    archive.write_bytes(b"not xz at all")
    (layout.archive_dir / sidecar_name(1)).unlink()

    report = _run(tmp_path, _policy())

    assert report.status == "corrupt", "the archive is unusable"
    assert report.exit_status == EXIT_CORRUPT, "an operator must act"
    assert report.failures == ((1, "bad_xz"),), "the fault is closed"
    assert not archive.exists(), "the damaged archive is moved aside"


def test_an_orphan_sidecar_is_quarantined(tmp_path: Path) -> None:
    """A sidecar with no archive cannot describe retained history."""
    layout = _layout(tmp_path)
    layout.archive_dir.mkdir(parents=True)
    orphan = layout.archive_dir / sidecar_name(9)
    orphan.write_text("{}")

    report = _run(tmp_path, _policy())

    assert report.status == "ok", "an orphan is a diagnostic, not a fault"
    assert [action.action for action in report.actions] == ["quarantined"], (
        "the orphan is moved aside"
    )
    assert not orphan.exists(), "nothing describes a missing archive"


def test_archiving_releases_every_sealed_source(tmp_path: Path) -> None:
    """The first run compresses every source and leaves the archive directory."""
    store = tmp_path / "cache-outcomes.jsonl"
    records = _records(NOW, 3)
    _write(store, records, _policy())
    layout = _layout(tmp_path)
    logical = sum(path.stat().st_size for path in layout.sealed_dir.glob("*.jsonl"))
    active = layout.active.stat().st_size

    report = _run(tmp_path, _policy())

    assert report.status == "ok", "the run succeeds"
    assert [action.action for action in report.actions] == ["archived"] * 3, (
        "each source is archived once"
    )
    assert not list(layout.sealed_dir.glob("*.jsonl")), "no source remains"
    assert len(_archives(tmp_path)) == 3, "every segment has an archive"
    assert report.retained_bytes == logical + active, (
        "the report counts logical history and the active header, not XZ bytes"
    )
    assert report.retained_bytes > sum(
        path.stat().st_size for path in _archives(tmp_path)
    ), "the XZ archives are smaller than the history they hold"


def test_a_second_run_finds_no_work(tmp_path: Path) -> None:
    """Archiving is idempotent: a repeat run neither rewrites nor deletes."""
    store = tmp_path / "cache-outcomes.jsonl"
    _write(store, _records(NOW, 2), _policy())
    _run(tmp_path, _policy())
    before = {path.name: path.read_bytes() for path in _archives(tmp_path)}

    report = _run(tmp_path, _policy())

    assert report.status == "ok", "the second run succeeds"
    assert not report.actions, "it has nothing to do"
    assert {path.name: path.read_bytes() for path in _archives(tmp_path)} == before, (
        "the archives are untouched"
    )


def test_age_eviction_settles_in_the_same_run(tmp_path: Path) -> None:
    """History archived by a run is evicted by that run when already too old."""
    store = tmp_path / "cache-outcomes.jsonl"
    old = NOW - dt.timedelta(days=40)
    _write(store, _records(old, 2), _policy())

    report = _run(tmp_path, _policy(max_age_days=30))

    assert report.status == "ok", "expiry is not a failure"
    assert [action.action for action in report.actions] == [
        "archived",
        "archived",
        "deleted",
        "deleted",
    ], "both sources are archived and then deleted"
    assert not _archives(tmp_path), "no expired history remains"
    assert (
        json.loads(_layout(tmp_path).retention_file.read_text(encoding="utf-8"))[
            "floor_sequence"
        ]
        == 0
    ), "the floor records an empty history"


def test_byte_eviction_deletes_the_oldest_archive_first(tmp_path: Path) -> None:
    """A byte budget smaller than the history removes whole oldest archives."""
    store = tmp_path / "cache-outcomes.jsonl"
    _write(store, _records(NOW, 3), _policy())
    _run(tmp_path, _policy())
    one_segment = read_inventory(_layout(tmp_path)).archived[0].logical_bytes
    layout = _layout(tmp_path)
    # The budget counts the active file's header too, so include it when
    # choosing an exact budget: only the oldest archive then has to go.
    active = layout.active.stat().st_size

    report = _run(tmp_path, _policy(max_bytes=one_segment * 2 + active))

    assert report.status == "ok", "eviction is a successful run"
    assert [
        action.sequence for action in report.actions if action.action == "deleted"
    ] == [1], "the oldest archive alone is evicted"
    assert [path.name for path in _archives(tmp_path)] == [
        archive_name(2),
        archive_name(3),
    ], "the newest two archives remain"
    assert layout.archive_dir.joinpath(sidecar_name(1)).exists() is False, (
        "the evicted sidecar is gone"
    )
    assert report.retained_bytes == one_segment * 2 + active, (
        "the budget is met exactly"
    )


def test_history_is_identical_before_and_after_archiving(tmp_path: Path) -> None:
    """Compression changes the encoding, never the replayable history."""
    store = tmp_path / "cache-outcomes.jsonl"
    records = _records(NOW, 4)
    _write(store, records, _policy())
    before = outcome_history.read_history_beside(store)

    _run(tmp_path, _policy())
    after = outcome_history.read_history_beside(store)

    assert len(before.records) == 4, "the store holds four rows"
    assert _observed(before) == _observed(after), "the order and rows are unchanged"
    assert before.header.salt_id == after.header.salt_id, "the salt domain survives"


@given(
    salt_id=st.text(alphabet="abc", min_size=1, max_size=4),
    counts=st.lists(st.integers(min_value=0, max_value=4), min_size=1, max_size=3),
)
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_archiving_preserves_every_generated_document(
    tmp_path: Path,
    salt_id: str,
    counts: list[int],
) -> None:
    """Every generated segment decompresses to exactly the bytes it held."""
    root = tmp_path / os.urandom(4).hex()
    layout = SegmentLayout.beside(root / "cache-outcomes.jsonl")
    layout.sealed_dir.mkdir(parents=True)
    documents: dict[int, bytes] = {}
    for sequence, count in enumerate(counts, start=1):
        document = _document(_records(NOW, count), salt_id)
        path = layout.sealed_dir / archive_name(sequence).removesuffix(".xz")
        path.write_bytes(document)
        path.chmod(0o600)
        documents[sequence] = document

    report = _run(root, _policy(max_age_days=0))

    assert report.status == "ok", "every generated document is archivable"
    assert len(_archives(root)) == len(counts), "one archive per segment"
    for sequence, document in documents.items():
        archived = layout.archive_dir / archive_name(sequence)
        assert lzma.decompress(archived.read_bytes()) == document, (
            "the archive holds the source bytes unchanged"
        )
    assert not read_inventory(layout).sealed, "no source is left behind"
