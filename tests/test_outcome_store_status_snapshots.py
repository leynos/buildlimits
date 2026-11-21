"""Status snapshots that refuse to pair a size with rows they cannot describe.

The writer publishes an ``ActiveHistory`` snapshot for the status read. When a
rollback leaves a fragment or the active file cannot be found, the size and the
row count describe different states, and the snapshot must say so rather than
present the pair as a complete measurement.
"""

import dataclasses as dc
import datetime as dt
import typing as typ
from pathlib import Path
from unittest import mock

from outcome_store_fixture_helpers import loaded_store_files
from outcome_test_helpers import STORE_EPOCH as NOW
from outcome_test_helpers import window_summary
from segment_test_helpers import history_policy

from buildlimits import outcome_records, outcome_store_io, outcome_store_state

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_history import ActiveHistory


def published_snapshot(files: outcome_store_state.StoreFiles) -> ActiveHistory:
    """Publish the worker's current state and return the status read.

    Returns
    -------
    ActiveHistory
        The snapshot a supervisor would see after this state.
    """
    files.publish_snapshot()
    return files.retention()


def assert_unpaired(snapshot: ActiveHistory, reason: str) -> None:
    """Require a snapshot to refuse to pair its size with its row count.

    Parameters
    ----------
    snapshot : ActiveHistory
        The reported active history.
    reason : str
        The closed reason the snapshot must name.
    """
    assert snapshot.has_metadata is False, f"the {reason} state is not a measured one"
    assert snapshot.reason == reason, f"the reason names {reason}"


def loaded_with_a_row(path: Path) -> outcome_store_state.StoreFiles:
    """Load an empty store and append one complete row to it.

    Returns
    -------
    StoreFiles
        The loaded writer state, holding the row it just appended.
    """
    files = loaded_store_files(path, policy=history_policy(1 << 20), now=NOW)
    record = window_summary()
    files.append(outcome_records.encode_line(record).encode("utf-8"), record)
    return files


def test_a_failed_rollback_is_never_paired_with_the_row_count(
    tmp_path: Path,
) -> None:
    """The status snapshot reports a fragment as an unpaired measurement.

    The writer cannot remove a fragment its rollback could not truncate until
    its next compaction, so the status read must mark the size incomplete
    rather than present it beside a row count it does not describe.
    """
    path = tmp_path / "cache-outcomes.jsonl"
    files = loaded_store_files(path, policy=history_policy(1 << 20), now=NOW)
    record = window_summary()
    line = outcome_records.encode_line(record).encode("utf-8")

    def tear(target: Path, _line: bytes) -> None:
        """Leave half a line behind, then report the failed rollback."""
        with target.open("ab") as handle:
            handle.write(b'{"format":')
        raise outcome_store_io.TornTailError("rollback refused")

    with mock.patch.object(outcome_store_state, "append_line", tear):
        files.append(line, record)
    unrepaired = published_snapshot(files)

    assert_unpaired(unrepaired, "repair_pending")
    assert unrepaired.records == 0, "the refused row is not counted"
    assert unrepaired.bytes == path.stat().st_size, (
        "the fragment is still part of the file's measured size"
    )

    assert files.compact() is True, "the next compaction repairs the file"
    repaired = published_snapshot(files)

    assert repaired.has_metadata is True, "the repaired file is one state again"
    assert repaired.records == 0, "the refused row was never retained"
    assert repaired.bytes == path.stat().st_size, "the size is the new file's"
    assert repaired.bytes < unrepaired.bytes, "the fragment is gone"


def test_a_missing_active_file_is_not_reported_as_an_empty_one(
    tmp_path: Path,
) -> None:
    """A file the writer cannot find is not a complete zero-byte state.

    A seal moves the active file into the sealed directory before it installs
    the replacement. If that installation fails, the writer still holds rows
    while the path holds no file at all, so the published snapshot must mark
    the state incomplete rather than pair a zero-byte size with those rows.
    """
    path = tmp_path / "cache-outcomes.jsonl"
    files = loaded_with_a_row(path)
    path.unlink()

    snapshot = published_snapshot(files)
    assert_unpaired(snapshot, "unreadable")


def test_an_idle_expiry_reports_the_compaction_it_performed(tmp_path: Path) -> None:
    """The worker republishes only when the age bound rewrote the file."""
    path = tmp_path / "cache-outcomes.jsonl"
    clock = [NOW]
    base = history_policy(1 << 20)
    files = loaded_store_files(
        path,
        policy=dc.replace(base, max_age=dt.timedelta(hours=1)),
        now=NOW,
        wall_clock=lambda: clock[0],
    )
    record = window_summary(observed_at=NOW)
    files.append(outcome_records.encode_line(record).encode("utf-8"), record)
    clock[0] = NOW + dt.timedelta(hours=2)

    assert files.expire_if_aged() is True, "the aged row is compacted away"
    assert files.expire_if_aged() is False, "a compacted store has nothing to expire"
