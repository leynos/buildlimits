"""The durable record that retention removed one sequence.

The record exists so a crash between an unlink and the retention floor write
does not turn an intentional removal into a permanent gap. These tests pin
that it is durable before the unlink, that a recorded floor clears it, and
that a marker cannot excuse a sequence the store still holds.
"""

import datetime as dt
import typing as typ
from pathlib import Path

from outcome_test_helpers import records_at
from segment_test_helpers import document, store_layout, write_private

from buildlimits import outcome_archive_state
from buildlimits.outcome_archive_fs import ensure_private_directory
from buildlimits.outcome_archive_run import RunOutcome
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segment_removed import (
    clear_covered,
    record_removal,
    removed_sequences,
)
from buildlimits.outcome_segment_state import RetentionFloor
from buildlimits.outcome_segments import removed_marker_name, segment_name

if typ.TYPE_CHECKING:
    from buildlimits.outcome_segments import SegmentLayout

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)


def _private_archive(root: Path) -> SegmentLayout:
    """Return one store layout whose archive directory exists and is private.

    Returns
    -------
    SegmentLayout
        The layout a marker is written into.
    """
    files = store_layout(root)
    ensure_private_directory(files.archive_dir)
    return files


def _sealed_source(root: Path, sequence: int) -> SegmentLayout:
    """Write one sealed source and return the layout it lives under.

    Returns
    -------
    SegmentLayout
        The store layout the source belongs to.
    """
    files = _private_archive(root)
    ensure_private_directory(files.sealed_dir)
    write_private(
        files.sealed_dir / segment_name(sequence),
        document(records_at(NOW, 1)),
    )
    return files


def _recomputed_floor(files: SegmentLayout) -> int:
    """Return the floor a run with no actions of its own would record.

    Returns
    -------
    int
        The floor derived from the store's inventory and removal markers.
    """
    return outcome_archive_state.retention_floor(
        read_inventory(files, retention_floor=1),
        RetentionFloor(sequence=1, state="recorded"),
        RunOutcome(),
        removed_sequences(files.archive_dir),
    )


def test_a_recorded_removal_is_durable_before_any_floor_is_written(
    tmp_path: Path,
) -> None:
    """The marker survives on its own, naming only the sequence."""
    files = _private_archive(tmp_path)

    record_removal(files.archive_dir, 3)

    assert removed_sequences(files.archive_dir) == frozenset({3}), (
        "the marker names the removed sequence"
    )
    assert (files.archive_dir / removed_marker_name(3)).exists(), (
        "the record is a file, so it outlives the process"
    )
    assert not files.retention_file.exists(), "no floor was written yet"


def test_a_recorded_removal_keeps_its_sequence_from_being_a_gap(
    tmp_path: Path,
) -> None:
    """An intentional removal is not missing history."""
    files = _sealed_source(tmp_path, 2)
    record_removal(files.archive_dir, 1)

    inventory = read_inventory(files, retention_floor=1)

    assert not inventory.gaps, "the recorded removal explains the absence"


def test_a_marker_for_a_present_sequence_does_not_advance_the_floor(
    tmp_path: Path,
) -> None:
    """A failed unlink leaves a marker that must not excuse a gap."""
    files = _sealed_source(tmp_path, 1)
    _sealed_source(tmp_path, 2)
    record_removal(files.archive_dir, 1)

    assert _recomputed_floor(files) == 1, "the held sequence is not removed"


def test_a_completed_run_clears_only_the_markers_a_floor_explains(
    tmp_path: Path,
) -> None:
    """Markers at or above the floor still stand for unreported removals."""
    files = _private_archive(tmp_path)
    for sequence in (1, 2, 4):
        record_removal(files.archive_dir, sequence)

    clear_covered(files.archive_dir, 2)

    assert removed_sequences(files.archive_dir) == frozenset({2, 4}), (
        "the marker at the floor is kept, because the floor does not cover it"
    )


def test_clearing_a_missing_directory_does_nothing(tmp_path: Path) -> None:
    """A store that never wrote a marker has none to clear."""
    files = store_layout(tmp_path)

    clear_covered(files.archive_dir, 2)

    assert removed_sequences(files.archive_dir) == frozenset(), (
        "an absent archive directory holds no markers"
    )


def test_a_temporary_beside_the_markers_is_not_a_removal(tmp_path: Path) -> None:
    """Only a name this code writes is evidence of an intentional removal."""
    files = _private_archive(tmp_path)
    (files.archive_dir / f"{removed_marker_name(6)}.tmp").write_text(
        "{}", encoding="utf-8"
    )

    assert removed_sequences(files.archive_dir) == frozenset(), (
        "a temporary's name is not a marker"
    )


def test_a_run_outcome_without_deletions_leaves_the_floor_alone(
    tmp_path: Path,
) -> None:
    """A run that removed nothing derives the floor it recorded."""
    files = _private_archive(tmp_path)

    assert _recomputed_floor(files) == 1, "no deletion and no marker advance it"
