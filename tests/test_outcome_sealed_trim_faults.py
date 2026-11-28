"""Failures and interruption while retention rewrites a sealed source.

An in-place rewrite publishes in four steps: verify a temporary, write the
marker, rename the source, write its metadata, remove the marker. These tests
stop the sequence at each step and pin that the store is left in a state the
next run repairs, that a full filesystem is retryable, and that a removal which
completed before a later fault is still recorded as a deletion.
"""

import datetime as dt
import errno
import json
import typing as typ
from pathlib import Path

from outcome_archive_helpers import (
    NOW,
    age_policy,
    document,
    layout,
    policy,
    refuse,
    refuse_removal_without_unlinking,
    retain,
)
from outcome_sealed_helpers import sealed_source
from outcome_test_helpers import attributed_record, records_at
from segment_test_helpers import write_private

from buildlimits import outcome_sealed_trim
from buildlimits.outcome_archive_verify import ArchiveFacts
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segments import (
    SEALED_TEMP_PREFIX,
    removed_marker_name,
    sealed_marker_name,
    sealed_meta_name,
    segment_name,
)

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_record_format import StoreRecord

CUTOFF = NOW - dt.timedelta(days=30)


def sealed_files(root: Path) -> set[str]:
    """Return the names the sealed directory holds.

    Returns
    -------
    set[str]
        Every file name in the sealed directory.
    """
    directory = layout(root).sealed_dir
    return {path.name for path in directory.iterdir()}


def straddling_rows() -> tuple[StoreRecord, StoreRecord]:
    """Return one expired row followed by one retained row.

    Returns
    -------
    tuple[StoreRecord, StoreRecord]
        The expired and retained rows, in file order.
    """
    return (
        attributed_record(observed_at=CUTOFF - dt.timedelta(days=1)),
        attributed_record(observed_at=NOW),
    )


def test_a_full_filesystem_leaves_the_source_for_the_next_firing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ENOSPC while writing the temporary is retryable, not corruption."""
    expired, fresh = straddling_rows()
    sealed_source(tmp_path, 1, (expired, fresh))

    def refuse(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Fail the temporary's creation as a full filesystem would."""
        raise OSError(errno.ENOSPC, "injected full filesystem")

    monkeypatch.setattr(outcome_sealed_trim, "open_private_temp", refuse)

    report = retain(tmp_path, age_policy())

    assert report.status == "no_headroom", "the timer retries the firing"
    assert report.failures == ((1, "no_headroom"),), "the closed reason is recorded"
    assert (layout(tmp_path).sealed_dir / segment_name(1)).read_bytes() == document((
        expired,
        fresh,
    )), "the source is untouched"
    assert not (layout(tmp_path).sealed_dir / sealed_marker_name(1)).exists(), (
        "no marker is written before a verified temporary exists"
    )


def test_a_verification_failure_keeps_the_source_and_removes_the_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temporary that scans back differently is this run's own output."""
    expired, fresh = straddling_rows()
    sealed_source(tmp_path, 1, (expired, fresh))

    def mismatched(_path: Path) -> ArchiveFacts:
        """Describe bytes no source was written from."""
        return ArchiveFacts(
            salt_id="salt-1",
            record_count=99,
            logical_bytes=99,
            sha256="b" * 64,
            first_observed_at=None,
            last_observed_at=None,
            had_torn_tail=False,
        )

    monkeypatch.setattr(outcome_sealed_trim, "read_source_facts", mismatched)

    report = retain(tmp_path, age_policy())

    assert report.failures == ((1, "unverified_plain"),), (
        "the output fault is named, not the readable source"
    )
    assert (layout(tmp_path).sealed_dir / segment_name(1)).read_bytes() == document((
        expired,
        fresh,
    )), "the source is untouched"
    assert sealed_files(tmp_path) == {segment_name(1), sealed_meta_name(1)}, (
        "the abandoned temporary is removed"
    )


def test_an_interruption_after_the_rename_is_repaired_from_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new file under stale metadata is repaired by the next firing."""
    expired, fresh = straddling_rows()
    sealed_source(tmp_path, 1, (expired, fresh))

    monkeypatch.setattr(outcome_sealed_trim, "write_sealed_metadata", refuse)
    first = retain(tmp_path, age_policy())
    monkeypatch.undo()

    assert first.status == "corrupt", "the interruption is reported"
    assert (layout(tmp_path).sealed_dir / segment_name(1)).read_bytes() == document((
        fresh,
    )), "the trimmed file is already published"
    assert (layout(tmp_path).sealed_dir / sealed_marker_name(1)).exists(), (
        "the marker covers the stale metadata"
    )
    assert read_inventory(layout(tmp_path)).sealed_replacing == (1,), (
        "the inventory refuses the marked source's metadata"
    )

    second = retain(tmp_path, age_policy())

    assert second.status == "ok", "the repair settles the store"
    metadata = json.loads(
        (layout(tmp_path).sealed_dir / sealed_meta_name(1)).read_text(encoding="utf-8")
    )
    assert metadata["record_count"] == 1, "the metadata is re-derived from the file"
    assert not (layout(tmp_path).sealed_dir / sealed_marker_name(1)).exists(), (
        "the marker is removed once the metadata is durable"
    )


def test_an_interruption_before_the_rename_keeps_the_original_rows(
    tmp_path: Path,
) -> None:
    """A marker written before the rename describes a source still intact."""
    rows = (
        attributed_record(observed_at=NOW - dt.timedelta(hours=2)),
        attributed_record(observed_at=NOW),
    )
    sealed_source(tmp_path, 1, rows)
    marker = layout(tmp_path).sealed_dir / sealed_marker_name(1)
    write_private(marker, b'{"schema":1,"sequence":1}\n')

    report = retain(tmp_path, age_policy())

    assert report.status == "ok", "the repair settles the interrupted rewrite"
    assert (layout(tmp_path).sealed_dir / segment_name(1)).read_bytes() == document(
        rows
    ), "the untrimmed source is re-described, not replaced"
    metadata = json.loads(
        (layout(tmp_path).sealed_dir / sealed_meta_name(1)).read_text(encoding="utf-8")
    )
    assert metadata["record_count"] == 2, "the metadata is re-derived from the file"
    assert not marker.exists(), "the stale marker is cleared"


def test_a_marker_without_its_source_is_removed(tmp_path: Path) -> None:
    """A removal that finished before its marker went leaves only the marker."""
    marker = layout(tmp_path).sealed_dir / sealed_marker_name(4)
    write_private(marker, b'{"schema":1,"sequence":4}\n')
    layout(tmp_path).active.parent.mkdir(parents=True, exist_ok=True)

    report = retain(tmp_path, age_policy())

    assert report.status == "ok", "a marker with no history is not a failure"
    assert not marker.exists(), "the marker is removed"
    assert [
        (action.action, action.sequence, action.reason) for action in report.actions
    ] == [("repaired", 4, "stale_marker")], "the cleanup is visible in the report"


def test_a_removal_that_completed_before_a_fault_is_recorded_as_a_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows that are already gone advance the floor even when a sync fails.

    A byte allowance too small to keep even one row turns the trim into a
    removal, which is the path that can complete before a later step fails.
    """
    rows = (
        attributed_record(observed_at=NOW - dt.timedelta(hours=1)),
        attributed_record(observed_at=NOW),
    )
    sealed_source(tmp_path, 1, rows)

    # The removal helper is patched where the trim looks it up, so the
    # replacement stands in for the deletion the trim itself performs.

    def remove_then_fail(directory: Path, sequence: int) -> None:
        """Delete the file as the real call would, then fail its durability."""
        (directory / segment_name(sequence)).unlink()
        raise OSError(errno.EIO, "injected fsync failure")

    monkeypatch.setattr(outcome_sealed_trim, "remove_sealed_files", remove_then_fail)

    report = retain(tmp_path, policy(1_000_000, max_age_days=0, max_bytes=1))

    assert report.failures == ((1, "unreadable"),), "the fault is still reported"
    assert [(action.action, action.reason) for action in report.actions] == [
        ("deleted", "bytes")
    ], "the completed removal is recorded with the bound that asked for it"
    floor = json.loads(layout(tmp_path).retention_file.read_text(encoding="utf-8"))
    assert floor["floor_sequence"] == 2, (
        "the floor advances over the sequence that was really removed"
    )


def test_a_failed_unlink_withdraws_the_marker_it_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that survives its failed removal must not keep a marker.

    The marker is published before the unlink, so a removal that stops there
    would otherwise explain a later loss of the same sequence.
    """
    rows = (
        attributed_record(observed_at=NOW - dt.timedelta(hours=1)),
        attributed_record(observed_at=NOW),
    )
    sealed_source(tmp_path, 1, rows)

    monkeypatch.setattr(
        outcome_sealed_trim, "remove_sealed_files", refuse_removal_without_unlinking
    )

    report = retain(tmp_path, policy(1_000_000, max_age_days=0, max_bytes=1))

    assert report.failures == ((1, "unreadable"),), "the fault is reported"
    assert not (layout(tmp_path).archive_dir / removed_marker_name(1)).exists(), (
        "the surviving source withdraws the marker"
    )
    assert (layout(tmp_path).sealed_dir / segment_name(1)).exists(), (
        "the source itself is untouched"
    )


def test_a_marker_left_by_a_crash_before_its_unlink_is_withdrawn(
    tmp_path: Path,
) -> None:
    """A restart reconciles a marker whose sequence is still present."""
    from buildlimits.outcome_archive_fs import ensure_private_directory
    from buildlimits.outcome_segment_removed import record_removal

    sealed_source(tmp_path, 1, records_at(NOW - dt.timedelta(hours=1), 1))
    files = layout(tmp_path)
    ensure_private_directory(files.archive_dir)
    record_removal(files.archive_dir, 1)

    report = retain(tmp_path, policy(1_000_000, max_age_days=0, max_bytes=0))

    assert report.status == "ok", "the reconciliation is not a failure"
    assert not (files.archive_dir / removed_marker_name(1)).exists(), (
        "the present source withdraws the marker on the next run"
    )


def test_a_corrupt_unmeasured_source_is_quarantined(tmp_path: Path) -> None:
    """A source whose metadata is missing and whose rows do not decode is moved."""
    directory = layout(tmp_path).sealed_dir
    write_private(directory / segment_name(1), b"not a json lines document\n")

    report = retain(tmp_path, age_policy())

    assert report.status == "corrupt", "the fault needs an operator"
    assert [(action.action, action.reason) for action in report.actions] == [
        ("quarantined", "corrupt")
    ], "the unreadable source is quarantined"
    assert not (directory / segment_name(1)).exists(), "it leaves the sealed directory"
    assert list(layout(tmp_path).quarantine_dir.iterdir()), "the evidence is preserved"


def test_the_cleanup_removes_only_maintenance_temporaries(tmp_path: Path) -> None:
    """The writer's own metadata temporary survives a maintenance pass."""
    expired, fresh = straddling_rows()
    sealed_source(tmp_path, 1, (expired, fresh))
    directory = layout(tmp_path).sealed_dir
    ours = write_private(
        directory / f".{SEALED_TEMP_PREFIX}-{segment_name(9)}.0123456789abcdef.tmp",
        b"abandoned",
    )
    writer = write_private(directory / f".{sealed_meta_name(2)}.tmp", b"in progress")

    report = retain(tmp_path, age_policy())

    assert report.status == "ok", "the pass completes"
    assert not ours.exists(), "the abandoned maintenance temporary is removed"
    assert writer.exists(), "the writer's temporary is left alone"


def test_a_trim_meeting_a_corrupt_source_quarantines_it(tmp_path: Path) -> None:
    """A row a planned trim cannot decode moves the source to quarantine."""
    import json

    from buildlimits.outcome_store_documents import sealed_metadata

    directory = layout(tmp_path).sealed_dir
    path = write_private(directory / segment_name(1), document(()) + b"{bad}\n{}\n")
    metadata = sealed_metadata(list(records_at(NOW, 2)), 1, "salt-1", NOW)
    (path.parent / sealed_meta_name(1)).write_text(
        json.dumps(metadata), encoding="utf-8"
    )

    report = retain(tmp_path, policy(1_000_000, max_age_days=0, max_bytes=1))

    assert report.status == "corrupt", "the fault needs an operator"
    assert [(action.action, action.reason) for action in report.actions] == [
        ("quarantined", "corrupt")
    ], "the source is quarantined rather than trimmed"


def test_a_temporary_that_cannot_be_scanned_is_the_runs_own_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault reading back the verified temporary names this run's output."""
    from buildlimits.outcome_archive_rows import SegmentFaultError

    expired, fresh = straddling_rows()
    source = sealed_source(tmp_path, 1, (expired, fresh))

    def unreadable(_path: Path) -> typ.NoReturn:
        """Refuse the verification pass over the temporary."""
        raise SegmentFaultError("truncated_xz")

    monkeypatch.setattr(outcome_sealed_trim, "read_source_facts", unreadable)

    report = retain(tmp_path, age_policy())

    assert report.failures == ((1, "unverified_plain"),), (
        "the output fault is named, not the readable source"
    )
    assert source.read_bytes() == document((expired, fresh)), "the source is untouched"
