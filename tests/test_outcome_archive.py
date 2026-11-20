"""End-to-end archive runs: statuses, dry runs and damaged stores.

The archiver plans, compresses, verifies and reports in one timer firing.
These tests build real segments with the writer and assert the observable
contract a timer depends on: closed exit statuses, a dry run that changes
nothing, a lock that reports busy, and damaged files that are quarantined
rather than silently trusted.
"""

import datetime as dt
import json
import typing as typ
from pathlib import Path

from outcome_archive_helpers import NOW, archives, layout, policy, records, run, write
from segment_test_helpers import sidecar_document, write_private

from buildlimits import outcome_archive_quarantine
from buildlimits.outcome_archive import (
    EXIT_CORRUPT,
    EXIT_OK,
    EXIT_TEMPFAIL,
    inspect_store,
)
from buildlimits.outcome_archive_lock import ArchiveLock
from buildlimits.outcome_segments import archive_name, segment_name, sidecar_name

if typ.TYPE_CHECKING:
    import pytest


def test_an_empty_store_records_a_successful_run(tmp_path: Path) -> None:
    """A timer firing on a store with no history still writes its state."""
    report = run(tmp_path, policy())

    store = layout(tmp_path)
    assert report.status == "ok", "an empty run is successful"
    assert report.exit_status == EXIT_OK, "the timer sees success"
    assert not report.actions, "there is nothing to do"
    assert report.retained_bytes == 0, "no history is retained"
    assert not report.gaps, "an empty store has no gaps"
    retention = json.loads(store.retention_file.read_text(encoding="utf-8"))
    last_run = json.loads(store.last_run_file.read_text(encoding="utf-8"))
    assert retention["floor_sequence"] == 0, "the floor records no history"
    assert retention["schema"] == 1, "the state schema is closed"
    assert last_run["status"] == "ok", "the last run is the same report"
    assert last_run["schema"] == 1, "the report schema is closed"


def test_a_dry_run_changes_nothing(tmp_path: Path) -> None:
    """A plan may be inspected without taking the lock or writing a file."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 2), policy())

    report = run(tmp_path, policy(), dry_run=True)

    store_layout = layout(tmp_path)
    assert report.dry_run, "the report says no file changed"
    assert report.plan is not None, "a dry run reports the plan it would run"
    assert report.plan.archive == 2, "the plan counts both sealed sources"
    assert len(list(store_layout.sealed_dir.glob("*.jsonl"))) == 2, (
        "both sources remain sealed"
    )
    assert not store_layout.archive_dir.exists(), "no archive state is created"
    assert not store_layout.retention_file.exists(), "no retention state is written"


def test_inspect_is_a_dry_run(tmp_path: Path) -> None:
    """The inspect entry point never mutates the store."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())

    report = inspect_store(layout(tmp_path), policy())

    assert report.dry_run, "inspection is always a dry run"
    assert report.plan is not None, "inspection reports the plan it would run"
    assert report.plan.archive == 1, "the pending source is reported"
    assert not archives(tmp_path), "a dry run publishes nothing"


def test_a_held_lock_reports_a_retryable_status(tmp_path: Path) -> None:
    """A timer firing during another run exits 75 instead of waiting."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    store_layout = layout(tmp_path)

    with ArchiveLock(store_layout.lock_file):
        report = run(tmp_path, policy())

    assert report.status == "busy", "the run did not start"
    assert report.exit_status == EXIT_TEMPFAIL, "a retry may succeed"
    assert report.reason == "busy", "the reason is closed"
    assert not archives(tmp_path), "a busy run publishes nothing"


def test_a_run_plans_the_store_it_finds_under_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A segment sealed while the run waited for the lock is still archived.

    Planning before the lock would leave that source for the next timer
    firing, so the run would report work it did not do.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    enter = ArchiveLock.__enter__

    def enter_after_sealing(lock: ArchiveLock) -> ArchiveLock:
        held = enter(lock)
        write(store, records(NOW, 1), policy())
        return held

    monkeypatch.setattr(ArchiveLock, "__enter__", enter_after_sealing)

    report = run(tmp_path, policy())

    assert report.plan is not None, "the report carries its plan"
    assert report.plan.archive == 2, "the plan covers the segment sealed meanwhile"
    assert len(archives(tmp_path)) == 2, "both sealed sources are archived"


def test_a_failed_orphan_quarantine_does_not_stop_eviction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable orphan is reported while the rest of the plan still runs.

    Abandoning the run would leave expired history retained and keep the timer
    retrying work it had already planned.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW - dt.timedelta(days=40), 2), policy())
    store_layout = layout(tmp_path)
    store_layout.archive_dir.mkdir(parents=True)
    write_private(
        store_layout.archive_dir / sidecar_name(9),
        json.dumps(sidecar_document(9)).encode(),
    )

    def refuse(*_args: object, **_kwargs: object) -> str:
        raise OSError("the orphan cannot be moved")

    monkeypatch.setattr(outcome_archive_quarantine, "quarantine_file", refuse)

    report = run(tmp_path, policy(max_age_days=30))

    assert (9, "unreadable") in report.failures, "the orphan failure is reported"
    assert [action.action for action in report.actions].count("deleted") == 2, (
        "both expired archives are still evicted"
    )
    assert not archives(tmp_path), "expired history does not outlive the failure"
    assert report.status == "corrupt", "the run names the fault it met"


def test_insufficient_headroom_reports_a_retryable_status(tmp_path: Path) -> None:
    """A full filesystem defers the run rather than failing it permanently."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())

    report = run(tmp_path, policy(headroom_bytes=1 << 62))

    assert report.status == "no_headroom", "the run did not start"
    assert report.exit_status == EXIT_TEMPFAIL, "the run is retryable"
    assert report.reason == "no_headroom", "the reason is closed"


def test_a_corrupt_source_is_quarantined_and_reported(tmp_path: Path) -> None:
    """A segment that cannot be decoded leaves the store and is named."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    store_layout = layout(tmp_path)
    source = next(store_layout.sealed_dir.glob("*.jsonl"))
    source.write_bytes(b"not a header\n")

    report = run(tmp_path, policy())

    assert report.status == "corrupt", "the run reports corruption"
    assert report.exit_status == EXIT_CORRUPT, "an operator must act"
    assert report.failures == ((1, "corrupt"),), "the sequence and reason are named"
    assert [action.action for action in report.actions] == ["quarantined"], (
        "the source is moved aside"
    )
    assert not archives(tmp_path), "no archive is published"
    assert list(store_layout.quarantine_dir.iterdir()), "the source is preserved"


def test_a_failed_source_quarantine_is_recorded_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed move of damaged evidence cannot abandon the rest of the plan."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 2), policy())
    store_layout = layout(tmp_path)
    damaged = store_layout.sealed_dir / segment_name(1)
    damaged.write_bytes(b"not a header\n")
    damaged.chmod(0o600)

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise OSError("the source cannot be moved")

    monkeypatch.setattr(outcome_archive_quarantine, "quarantine_source", refuse)

    report = run(tmp_path, policy())

    assert (1, "unreadable") in report.failures, "the failed move is reported"
    assert [action.sequence for action in report.actions] == [2], (
        "the healthy source is still archived"
    )
    assert damaged.exists(), "the damaged source stays until it can be moved"
    assert report.status == "corrupt", "the run names the fault it met"


def test_a_missing_sidecar_is_repaired(tmp_path: Path) -> None:
    """A sidecar lost to a crash is rebuilt from the verified archive."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    run(tmp_path, policy())
    store_layout = layout(tmp_path)
    (store_layout.archive_dir / sidecar_name(1)).unlink()

    report = run(tmp_path, policy())

    assert report.status == "ok", "healing is not a failure"
    assert [action.action for action in report.actions] == ["repaired"], (
        "the sidecar is rebuilt"
    )
    assert (store_layout.archive_dir / sidecar_name(1)).exists(), "the sidecar is back"


def test_a_damaged_archive_cannot_be_healed(tmp_path: Path) -> None:
    """An archive that no longer verifies is quarantined, not described."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    run(tmp_path, policy())
    store_layout = layout(tmp_path)
    archive = store_layout.archive_dir / archive_name(1)
    archive.write_bytes(b"not xz at all")
    (store_layout.archive_dir / sidecar_name(1)).unlink()

    report = run(tmp_path, policy())

    assert report.status == "corrupt", "the archive is unusable"
    assert report.exit_status == EXIT_CORRUPT, "an operator must act"
    assert report.failures == ((1, "bad_xz"),), "the fault is closed"
    assert not archive.exists(), "the damaged archive is moved aside"


def test_an_orphan_sidecar_is_quarantined(tmp_path: Path) -> None:
    """A sidecar with no archive cannot describe retained history."""
    store_layout = layout(tmp_path)
    store_layout.archive_dir.mkdir(parents=True)
    orphan = store_layout.archive_dir / sidecar_name(9)
    orphan.write_text("{}")

    report = run(tmp_path, policy())

    assert report.status == "ok", "an orphan is a diagnostic, not a fault"
    assert [action.action for action in report.actions] == ["quarantined"], (
        "the orphan is moved aside"
    )
    assert not orphan.exists(), "nothing describes a missing archive"
