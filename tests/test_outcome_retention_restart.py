"""What a later retention firing sees after the store has settled.

A trim publishes metadata that describes the file it wrote, so a restart finds
nothing to repeat; a lowered bound reaches history that was already retained;
and a removal whose floor write never happened is still explained by the
record the deletion published first.
"""

import datetime as dt
import json
from pathlib import Path

from outcome_archive_helpers import NOW, document, layout, policy, retain
from outcome_sealed_helpers import sealed_bytes, sealed_source
from outcome_test_helpers import attributed_record, records_at

from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segments import sealed_meta_name, segment_name

CUTOFF = NOW - dt.timedelta(days=30)


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


def test_a_removal_record_left_by_a_crash_is_not_reported_as_lost(
    tmp_path: Path,
) -> None:
    """A sequence removed before the floor write is not missing history.

    The removal record is published before the unlink, so a process that
    stopped before installing its floor leaves the next firing evidence that
    retention removed the sequence. Without it the store would report the gap
    on every later firing, because the source it names no longer exists.
    """
    from buildlimits.outcome_archive_fs import ensure_private_directory
    from buildlimits.outcome_segment_removed import record_removal
    from buildlimits.outcome_segments import removed_marker_name

    sealed_source(tmp_path, 1, records_at(NOW - dt.timedelta(hours=2), 1))
    sealed_source(tmp_path, 2, records_at(NOW - dt.timedelta(hours=1), 1))
    crashed = layout(tmp_path)
    ensure_private_directory(crashed.archive_dir)
    record_removal(crashed.archive_dir, 1)
    (crashed.sealed_dir / segment_name(1)).unlink()
    (crashed.sealed_dir / sealed_meta_name(1)).unlink()

    report = retain(tmp_path, policy(1_000_000))

    assert report.status == "ok", "the run settles"
    assert report.gaps == (), "the recorded removal is not reported as lost"
    floor = json.loads(crashed.retention_file.read_text(encoding="utf-8"))
    assert floor["floor_sequence"] == 2, "the floor advances past the removal"
    assert not (crashed.archive_dir / removed_marker_name(1)).exists(), (
        "the recorded floor explains the removal, so its marker is cleared"
    )
