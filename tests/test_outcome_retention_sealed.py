"""Retention enforced on closed history without compressing it.

The retention-only operation applies the same age, byte and record bounds the
archive run applies, but it rewrites a sealed straddler in place as plain JSON
Lines instead of compressing it first. These tests pin that the bounds act on
sealed and archived history together, that append order decides a byte or
record prefix, and that no XZ encoder runs when only sealed history is
involved.
"""

import datetime as dt
import json
import lzma
import typing as typ
from pathlib import Path

import pytest
from outcome_archive_helpers import NOW, document, layout, policy
from outcome_sealed_helpers import sealed_bytes, sealed_source, sealed_source_exists
from outcome_test_helpers import attributed_record, records_at
from segment_test_helpers import write_private

from buildlimits.outcome_archive import RunOptions, run_archive, run_retain
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segments import (
    archive_name,
    sealed_marker_name,
    sealed_meta_name,
    segment_name,
)

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_report import RunReport
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_retention import RetentionPolicy

CUTOFF = NOW - dt.timedelta(days=30)
"""The cutoff a thirty-day policy applies at the shared test moment."""

SEALED_AT = NOW - dt.timedelta(days=45)
"""The seal time the fixtures record, preserved across an in-place rewrite."""


def straddling_rows() -> tuple[StoreRecord, StoreRecord, StoreRecord]:
    """Return one expired, one at-cutoff and one fresh row, in file order.

    Returns
    -------
    tuple[StoreRecord, StoreRecord, StoreRecord]
        The three rows a thirty-day policy treats differently.
    """
    return (
        attributed_record(observed_at=CUTOFF - dt.timedelta(days=1)),
        attributed_record(observed_at=CUTOFF),
        attributed_record(observed_at=NOW),
    )


def sealed_metadata_of(root: Path, sequence: int) -> dict[str, object]:
    """Return the metadata document one sealed source is described by.

    Returns
    -------
    dict[str, object]
        The decoded metadata.
    """
    path = layout(root).sealed_dir / sealed_meta_name(sequence)
    return json.loads(path.read_text(encoding="utf-8"))


def retain(
    root: Path,
    retention: RetentionPolicy,
    *,
    now: dt.datetime = NOW,
    dry_run: bool = False,
) -> RunReport:
    """Run the retention-only operation once at a fixed moment.

    Returns
    -------
    RunReport
        The run report.
    """
    return run_retain(layout(root), retention, RunOptions(now=now, dry_run=dry_run))


def archive(
    root: Path,
    retention: RetentionPolicy,
    *,
    now: dt.datetime = NOW,
) -> RunReport:
    """Run the archiving operation once at a fixed moment.

    Returns
    -------
    RunReport
        The run report.
    """
    return run_archive(layout(root), retention, RunOptions(now=now))


def refuse_compression(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every XZ entry point fail if retention reaches one."""

    def refuse(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Fail the test: retention compressed history it should have kept plain."""
        pytest.fail("retention compressed history instead of rewriting it in place")

    monkeypatch.setattr(lzma, "LZMACompressor", refuse)
    monkeypatch.setattr(lzma, "compress", refuse)
    monkeypatch.setattr(lzma, "decompress", refuse)


def test_a_sealed_straddler_is_trimmed_in_place_without_compression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An age trim rewrites the plain source and keeps the row at the cutoff."""
    expired, at_cutoff, fresh = straddling_rows()
    sealed_source(tmp_path, 1, (expired, at_cutoff, fresh), sealed_at=SEALED_AT)
    refuse_compression(monkeypatch)

    report = retain(tmp_path, policy(1_000_000, max_age_days=30, max_bytes=0))

    assert report.status == "ok", "the in-place trim completes"
    assert report.operation == "retain", "the report names the operation"
    assert sealed_bytes(tmp_path, 1) == document((at_cutoff, fresh)), (
        "the expired row is gone and the row at the cutoff is retained"
    )
    metadata = sealed_metadata_of(tmp_path, 1)
    assert metadata["record_count"] == 2, "the metadata is re-derived"
    assert metadata["sealed_at"] == "2026-10-05T00:00:00Z", (
        "an in-place trim does not reset the source's seal time"
    )
    assert not list(layout(tmp_path).archive_dir.glob("*.xz")), (
        "no archive was created for the sealed source"
    )
    actions = [
        (action.action, action.sequence, action.reason) for action in report.actions
    ]
    assert ("trimmed", 1, "age") in actions, "the rewrite names the age bound"
    assert not read_inventory(layout(tmp_path)).sealed_replacing, (
        "the rewrite marker is removed once the metadata is durable"
    )


def test_a_whole_sealed_segment_is_deleted_without_compression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A segment whose every row is older than the cutoff is removed whole."""
    rows = (
        attributed_record(observed_at=CUTOFF - dt.timedelta(days=2)),
        attributed_record(observed_at=CUTOFF - dt.timedelta(days=1)),
    )
    sealed_source(tmp_path, 1, rows)
    refuse_compression(monkeypatch)

    report = retain(tmp_path, policy(1_000_000, max_age_days=30, max_bytes=0))

    assert report.status == "ok", "the deletion completes"
    sealed = layout(tmp_path).sealed_dir
    assert not (sealed / segment_name(1)).exists(), "the source is removed"
    assert not (sealed / sealed_meta_name(1)).exists(), "its metadata goes with it"
    assert [(action.action, action.reason) for action in report.actions] == [
        ("deleted", "age")
    ], "the deletion names the age bound"
    floor = json.loads(layout(tmp_path).retention_file.read_text(encoding="utf-8"))
    assert floor["floor_sequence"] == 2, (
        "the floor records that the sequence was removed on purpose"
    )


def test_a_byte_bound_evicts_a_whole_older_segment_and_trims_a_newer_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Append order decides: the older source goes whole, the newer is trimmed."""
    older = (attributed_record(observed_at=NOW - dt.timedelta(hours=4)),)
    newer = records_at(NOW - dt.timedelta(hours=3), 3)
    sealed_source(tmp_path, 1, older)
    sealed_source(tmp_path, 2, newer)
    refuse_compression(monkeypatch)
    # Room for the newer source's last two rows only: the older source is small
    # enough to fit inside the excess, so the walk removes it whole and trims
    # the newer source to its suffix.
    budget = len(document(newer[1:]))

    report = retain(tmp_path, policy(1_000_000, max_age_days=0, max_bytes=budget))

    assert report.status == "ok", "the byte bound is met"
    sealed = layout(tmp_path).sealed_dir
    assert not (sealed / segment_name(1)).exists(), "the older source is removed whole"
    assert sealed_bytes(tmp_path, 2) == document(newer[1:]), (
        "the newer source keeps its newest rows in append order"
    )
    causes = {(action.sequence, action.reason) for action in report.actions}
    assert (1, "bytes") in causes, "the whole deletion names the byte bound"
    assert (2, "bytes") in causes, "the prefix trim names the byte bound"


def test_a_record_bound_trims_a_sealed_source_by_append_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record cap drops the oldest rows, which are the leading ones."""
    rows = records_at(NOW - dt.timedelta(hours=1), 4)
    sealed_source(tmp_path, 1, rows)
    refuse_compression(monkeypatch)

    report = retain(tmp_path, policy(1_000_000, max_age_days=0, max_records=2))

    assert report.status == "ok", "the cap is met"
    assert sealed_bytes(tmp_path, 1) == document(rows[2:]), (
        "the two newest rows survive, in append order"
    )
    assert [(action.action, action.reason) for action in report.actions] == [
        ("trimmed", "records")
    ], "the rewrite names the record bound"


def test_active_and_closed_history_are_accounted_together(tmp_path: Path) -> None:
    """A sealed source and the writer's active rows share one record cap."""
    active = records_at(NOW - dt.timedelta(minutes=3), 2)
    write_private(layout(tmp_path).active, document(active))
    closed = records_at(NOW - dt.timedelta(hours=1), 2)
    sealed_source(tmp_path, 1, closed)

    report = retain(tmp_path, policy(1_000_000, max_age_days=0, max_records=3))

    assert report.status == "ok", "the cap is met"
    assert sealed_bytes(tmp_path, 1) == document(closed[1:]), (
        "one closed row goes so that three rows remain across both states"
    )
    assert report.retained_records == 3, "the report counts both states"
    assert layout(tmp_path).active.read_bytes() == document(active), (
        "the active file the writer owns is never rewritten"
    )


def test_an_existing_archive_stays_an_archive_when_retention_rewrites_it(
    tmp_path: Path,
) -> None:
    """A straddling archive is recompressed; it is never converted to plain."""
    expired, at_cutoff, fresh = straddling_rows()
    sealed_source(tmp_path, 1, (expired, at_cutoff, fresh))
    first = archive(tmp_path, policy(1_000_000, max_age_days=0, max_bytes=0))
    assert first.status == "ok", "the fixture archive is published untrimmed"

    report = retain(tmp_path, policy(1_000_000, max_age_days=30, max_bytes=0))

    assert report.status == "ok", "the archive trim completes"
    archive_path = layout(tmp_path).archive_dir / archive_name(1)
    assert lzma.decompress(archive_path.read_bytes()) == document((at_cutoff, fresh)), (
        "the archive keeps the rows the age rule retains"
    )
    assert not (layout(tmp_path).sealed_dir / segment_name(1)).exists(), (
        "no plain source is written back beside the archive"
    )


def test_a_retained_dry_run_changes_nothing(tmp_path: Path) -> None:
    """The preview reports the plan without rewriting, deleting or recording."""
    rows = records_at(CUTOFF - dt.timedelta(days=3), 2)
    sealed_source(tmp_path, 1, rows)
    before = sealed_bytes(tmp_path, 1)

    report = retain(
        tmp_path, policy(1_000_000, max_age_days=30, max_bytes=0), dry_run=True
    )

    assert report.dry_run is True, "the report says nothing was changed"
    assert report.plan is not None and report.plan.delete == 1, (
        "the preview plans the whole-segment eviction"
    )
    assert sealed_bytes(tmp_path, 1) == before, "the source is untouched"
    assert not (layout(tmp_path).sealed_dir / sealed_marker_name(1)).exists(), (
        "a preview writes no rewrite marker"
    )
    assert not layout(tmp_path).last_retain_file.exists(), (
        "a preview installs no report"
    )


def test_a_second_retain_run_after_a_trim_is_a_no_op(tmp_path: Path) -> None:
    """The rewritten metadata describes the file, so a restart settles at once."""
    expired = attributed_record(observed_at=CUTOFF - dt.timedelta(days=1))
    fresh = attributed_record(observed_at=NOW)
    sealed_source(tmp_path, 1, (expired, fresh))

    first = retain(tmp_path, policy(1_000_000, max_age_days=30, max_bytes=0))
    second = retain(tmp_path, policy(1_000_000, max_age_days=30, max_bytes=0))

    assert first.status == "ok", "the first run trims the source"
    assert second.status == "ok", "the second run finds nothing to do"
    assert second.actions == (), "a settled store needs no rewrite"
    inventory = read_inventory(layout(tmp_path))
    assert inventory.sealed[0].record_count == 1, (
        "a fresh inventory trusts the rewritten metadata"
    )


def test_a_reduced_bound_reaches_closed_history_at_the_next_retain_run(
    tmp_path: Path,
) -> None:
    """Lowering the byte budget evicts sealed history without an archive run."""
    rows = records_at(NOW - dt.timedelta(hours=2), 4)
    sealed_source(tmp_path, 1, rows)
    permissive = retain(tmp_path, policy(1_000_000, max_age_days=0, max_bytes=0))
    assert permissive.actions == (), "the permissive policy evicts nothing"

    reduced = retain(
        tmp_path, policy(1_000_000, max_age_days=0, max_bytes=len(document(rows[:1])))
    )

    assert reduced.status == "ok", "the reduced bound is met"
    assert sealed_bytes(tmp_path, 1) == document(rows[3:]), (
        "the budget keeps the newest row, because a prefix trim drops oldest first"
    )


def test_a_torn_tail_does_not_cost_a_complete_row_to_the_byte_allowance(
    tmp_path: Path,
) -> None:
    """The allowance is compared with the bytes the rewrite can actually keep.

    A sealed source can end with an unterminated fragment the reader drops. Its
    bytes are on disk but can never be written, so counting them against the
    allowance would drop complete rows that fit.
    """
    rows = records_at(NOW - dt.timedelta(hours=2), 2)
    path = sealed_source(tmp_path, 1, rows)
    path.write_bytes(path.read_bytes() + b"{torn")

    report = retain(
        tmp_path,
        policy(1_000_000, max_age_days=0, max_bytes=len(document(rows))),
    )

    assert report.status == "ok", "the allowance is met"
    assert sealed_bytes(tmp_path, 1) == document(rows), (
        "the fragment costs no complete row"
    )


def test_a_trim_that_keeps_no_record_removes_the_source(
    tmp_path: Path,
) -> None:
    """A byte allowance too small for one row leaves no header-only segment."""
    rows = records_at(NOW - dt.timedelta(hours=1), 2)
    sealed_source(tmp_path, 1, rows)

    report = retain(tmp_path, policy(1_000_000, max_age_days=0, max_bytes=1))

    assert report.status == "ok", "the removal completes"
    assert not sealed_source_exists(tmp_path, 1), "the source is gone"
    assert not (layout(tmp_path).sealed_dir / sealed_meta_name(1)).exists(), (
        "its metadata goes with it"
    )
    assert [(action.action, action.reason) for action in report.actions] == [
        ("deleted", "bytes")
    ], "the removal names the bound that asked for it"


def test_a_mismatched_duplicate_is_quarantined_and_the_source_kept(
    tmp_path: Path,
) -> None:
    """A retention pass never re-archives: the bad archive goes, the source stays."""
    from outcome_archive_helpers import run as archive_run
    from outcome_archive_helpers import write as write_store
    from outcome_sealed_helpers import sealed_source as measured_sealed_source

    store = tmp_path / "cache-outcomes.jsonl"
    write_store(store, records_at(NOW, 1), policy())
    archive_run(tmp_path, policy(1_000_000, max_age_days=0, max_bytes=0))
    measured_sealed_source(tmp_path, 1, records_at(NOW, 3))

    report = retain(tmp_path, policy(1_000_000, max_age_days=0, max_bytes=0))

    assert report.status == "ok", "the quarantine is not a run failure"
    assert [(action.action, action.reason) for action in report.actions] == [
        ("quarantined", "mismatch")
    ], "the unusable archive is quarantined"
    assert not (layout(tmp_path).archive_dir / archive_name(1)).exists(), (
        "the mismatched archive leaves the archive directory"
    )
    assert sealed_source_exists(tmp_path, 1), (
        "the sealed source is kept for a later plan"
    )
