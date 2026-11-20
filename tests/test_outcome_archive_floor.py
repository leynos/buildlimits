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

from buildlimits import outcome_archive, outcome_archive_fs
from buildlimits.outcome_archive_run import deleted_sequences

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
        mock.patch.object(outcome_archive, "_measure", archive.refuse),
        max_records=1,
    )

    assert failed.status == "failed", "the result could not be measured"
    assert failed.reason == "unreadable", "the reason is closed"
    assert deleted_sequences(failed.actions) == (1, 2), (
        "both deletions were performed before the read failed"
    )
    assert_floor_survives(tmp_path, recovered, 3)
