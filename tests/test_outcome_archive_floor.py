"""The retention floor survives a run that could not record its own state.

Both failure paths here have already deleted history before the run learns it
cannot describe the result. A later run that deletes nothing derives its floor
from ``retention.json`` alone, so each test requires the completed deletions
to be recorded anyway.
"""

import contextlib
import dataclasses as dc
import json
import typing as typ
from pathlib import Path
from unittest import mock

import outcome_archive_helpers as archive

from buildlimits import outcome_archive_fs, outcome_archive_state
from buildlimits.outcome_archive_run import deleted_sequences
from buildlimits.outcome_segments import archive_name, sidecar_name

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_report import RunReport


def persisted_floor(root: Path) -> int:
    """Return the floor sequence recorded in ``retention.json``.

    Returns
    -------
    int
        The floor the last run persisted.
    """
    document = json.loads(
        archive.layout(root).retention_file.read_text(encoding="utf-8")
    )
    return document["floor_sequence"]


def recover_after_failure(
    root: Path,
    failure: contextlib.AbstractContextManager[object],
    *,
    max_records: int,
) -> tuple[RunReport, RunReport]:
    """Run one capped pass under an injected fault, then the next firing.

    Both tests describe the same sequence: keep history unlimited, then run a
    capped pass that has deleted archives before it fails. Only the injected
    fault differs, so the fault is entered around the capped pass alone.

    Returns
    -------
    tuple[RunReport, RunReport]
        The failed pass and the recovery pass.
    """
    archive.seeded_store(root, archive.records(archive.NOW, 3))
    unlimited = archive.policy(max_bytes=0)
    archive.run(root, unlimited)
    capped = dc.replace(unlimited, max_records=max_records)
    with failure:
        failed = archive.run(root, capped)
    return failed, archive.run(root, capped)


def assert_floor_survives(root: Path, recovered: RunReport, floor: int) -> None:
    """Assert the next firing kept the recorded floor and reported no gap."""
    assert recovered.status == "ok", "the next firing completes normally"
    assert persisted_floor(root) == floor, "the floor advances past the deletions"
    assert not recovered.gaps, "the deletions are not reported as lost history"


def lose_archive(root: Path, sequence: int) -> None:
    """Remove one archive and its sidecar without recording a deletion."""
    directory = archive.layout(root).archive_dir
    for name in (archive_name(sequence), sidecar_name(sequence)):
        (directory / name).unlink()


def test_a_failed_state_write_still_records_the_deletions_it_completed(
    tmp_path: Path,
) -> None:
    """A later run must inherit deletions whose run could not save its state.

    ``retain`` has already removed the oldest archive when the state write
    fails. The floor is the only record that the sequence was deleted rather
    than lost, so the failed run gives it a second attempt. A later run with
    nothing left to delete then persists the advanced floor instead of
    reporting the deleted sequence as a gap for ever.
    """
    files = archive.layout(tmp_path)
    write_json = outcome_archive_fs.atomic_write_json
    attempts = 0

    def fail_first_floor_write(path: Path, document: dict[str, object]) -> None:
        """Refuse the first floor write, then delegate every later one."""
        nonlocal attempts
        if path == files.retention_file:
            attempts += 1
            if attempts == 1:
                archive.refuse()
        write_json(path, document)

    failed, recovered = recover_after_failure(
        tmp_path,
        mock.patch.object(
            outcome_archive_fs, "atomic_write_json", fail_first_floor_write
        ),
        max_records=2,
    )

    assert failed.status == "failed", "the timer must retry the state write"
    assert deleted_sequences(failed.actions) == (1,), (
        "the run deleted the oldest archive before the write failed"
    )
    assert_floor_survives(tmp_path, recovered, 2)


def test_a_primary_floor_that_stays_unwritable_falls_back_to_pending(
    tmp_path: Path,
) -> None:
    """A fault local to ``retention.json`` must not lose completed deletions.

    The retry after a failed state write can fail for the same reason the
    first write did, such as a directory squatting on the primary name. The
    floor is then written to the pending record, which readers merge with the
    primary one, so the deletions are still reported as intentional. The next
    successful run installs the floor in the primary record and removes the
    fallback.
    """
    files = archive.layout(tmp_path)
    write_json = outcome_archive_fs.atomic_write_json
    wrote_pending = False

    def fail_every_primary_write(path: Path, document: dict[str, object]) -> None:
        """Refuse every write to the primary record and keep the fallback."""
        nonlocal wrote_pending
        if path == files.retention_file:
            archive.refuse()
        wrote_pending = wrote_pending or path == files.pending_retention_file
        write_json(path, document)

    failed, recovered = recover_after_failure(
        tmp_path,
        mock.patch.object(
            outcome_archive_fs, "atomic_write_json", fail_every_primary_write
        ),
        max_records=2,
    )

    assert failed.status == "failed", "the timer must retry the state write"
    assert wrote_pending, "the floor has a fallback record"
    assert_floor_survives(tmp_path, recovered, 2)
    assert not files.pending_retention_file.exists(), (
        "the next successful run folds the fallback into the primary record"
    )


def test_an_unmeasurable_run_still_records_the_deletions_it_completed(
    tmp_path: Path,
) -> None:
    """The post-run read can fail after retention has already removed history.

    The deletions are real before the read fails, so they are recorded from
    the actions the run performed and the floor already on disk. A later run
    with nothing left to delete then keeps that floor instead of reporting
    both sequences as gaps for ever.
    """
    failed, recovered = recover_after_failure(
        tmp_path,
        mock.patch.object(outcome_archive_state, "retention_floor", archive.refuse),
        max_records=1,
    )

    assert failed.status == "failed", "the result could not be measured"
    assert failed.reason == "unreadable", "the reason is closed"
    assert deleted_sequences(failed.actions) == (1, 2), (
        "both deletions were performed before the read failed"
    )
    assert_floor_survives(tmp_path, recovered, 3)


def test_a_hole_below_a_deletion_stays_reportable(tmp_path: Path) -> None:
    """A deletion beyond a missing sequence must not excuse that sequence.

    Retention deletes the oldest archives, so a hole makes the run's deleted
    set non-contiguous. Raising the floor past the hole would let the next
    scan treat the missing sequence as intentionally evicted, so the floor
    stops at the hole and a later sealed source proves it is still reported.
    """
    store = archive.seeded_store(tmp_path, archive.records(archive.NOW, 3))
    unlimited = archive.policy(max_bytes=0)
    archive.run(tmp_path, unlimited)
    lose_archive(tmp_path, 2)

    archive.run(tmp_path, dc.replace(unlimited, max_records=0))

    assert persisted_floor(tmp_path) == 2, "the floor stops at the hole"
    archive.write(store, archive.records(archive.NOW, 1), unlimited)
    observed = archive.run(tmp_path, unlimited, dry_run=True)
    assert 2 in observed.gaps, "the missing sequence is still a reported gap"


def test_the_failure_floor_does_not_pass_a_hole(tmp_path: Path) -> None:
    """An unmeasurable run advances only across a contiguous run of deletions.

    The same hole and deletion set reach ``write_deleted_floor`` without a
    post-run inventory, so the stop must be derived from the recorded floor
    and the deletions alone.
    """
    archive.seeded_store(tmp_path, archive.records(archive.NOW, 3))
    unlimited = archive.policy(max_bytes=0)
    archive.run(tmp_path, unlimited)
    lose_archive(tmp_path, 2)

    with mock.patch.object(outcome_archive_state, "retention_floor", archive.refuse):
        failed = archive.run(tmp_path, dc.replace(unlimited, max_records=0))

    assert failed.status == "failed", "the result could not be measured"
    assert deleted_sequences(failed.actions) == (1, 3), (
        "retention removed the archives either side of the hole"
    )
    assert persisted_floor(tmp_path) == 2, "the floor stops at the hole"
