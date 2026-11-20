"""What the archiver accepts as the writer's active history.

The active file is read without the archive lock, so it is a snapshot rather
than a stable input: only an absent file means there is no active history. A
path that is not a regular file, one that cannot be inspected, and a file whose
rows cannot be decoded must not make the store look smaller than it is to the
byte budget or the record cap.
"""

import datetime as dt
import errno
import typing as typ
from pathlib import Path

import pytest
from outcome_archive_helpers import NOW, document, layout, policy, records, run, write
from segment_test_helpers import write_private

from buildlimits import outcome_archive_active, outcome_record_encode
from buildlimits.outcome_archive import EXIT_FAILED
from buildlimits.outcome_archive_active import active_history
from buildlimits.outcome_archive_dirs import UnsafePathError
from buildlimits.outcome_segments import segment_name

if typ.TYPE_CHECKING:
    import os

    from buildlimits.outcome_archive_report import RunReport
    from buildlimits.outcome_segments import SegmentLayout


def _assert_refused(report: RunReport, store_layout: SegmentLayout) -> None:
    """Assert one run refused the store without mutating it.

    A refused run must name a closed reason and a bounded exit status, leave
    the sealed source for a run that can read the store, and claim no action
    over history it could not measure.

    Parameters
    ----------
    report : RunReport
        The run's report.
    store_layout : SegmentLayout
        The layout of the store the run was given.
    """
    assert report.status == "failed", "an unknown active history is a run failure"
    assert report.reason == "unreadable", "the closed reason names the inspection"
    assert report.exit_status == EXIT_FAILED, "the timer does not treat it as a retry"
    assert not report.actions, "nothing is archived over an unknown history"
    assert store_layout.sealed_dir.joinpath(segment_name(1)).exists(), (
        "the sealed source is left for a run that can read the store"
    )


def test_a_symlinked_active_file_is_refused(tmp_path: Path) -> None:
    """A path that is not a regular file is never counted as active history.

    The archiver reads the active file to count the rows the writer has not
    sealed. A symlink there could name any file, so following it would let
    that file's rows and bytes decide retention. The active path must be a
    regular file before anything is counted, and a substituted path is a
    refusal rather than an empty history.
    """
    target = tmp_path / "elsewhere.jsonl"
    target.write_bytes(document(records(NOW, 3)))
    target.chmod(0o600)
    store = tmp_path / "cache-outcomes.jsonl"
    store.symlink_to(target)

    with pytest.raises(UnsafePathError):
        active_history(store)

    assert target.read_bytes() == document(records(NOW, 3)), (
        "the link target is left alone"
    )


def test_a_run_refuses_a_symlinked_active_file(tmp_path: Path) -> None:
    """A run reports a substituted active path instead of ignoring it.

    Counting the link as empty history would make the store look smaller than
    it is to both budgets, so the run stops before it mutates anything.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    store_layout = layout(tmp_path)
    target = tmp_path / "outside.jsonl"
    target.write_bytes(document(records(NOW, 6)))
    target.chmod(0o600)
    store_layout.active.unlink(missing_ok=True)
    store_layout.active.symlink_to(target)

    report = run(tmp_path, policy())

    _assert_refused(report, store_layout)
    assert target.read_bytes() == document(records(NOW, 6)), (
        "the link target is never folded into the store's history"
    )


def test_an_inaccessible_active_path_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that cannot be inspected is unknown history, not an empty file.

    Only an absent active file means the writer holds no rows. Reporting a
    failure to inspect it as zero bytes and zero records would let the store
    look empty to both budgets.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    store_layout = layout(tmp_path)
    real = outcome_archive_active.open_private_regular

    def refuse_active(
        target: Path, flags: int, *, enforce_mode: bool = True
    ) -> tuple[int, os.stat_result]:
        """Fail the inspection of the active path alone."""
        if target == store_layout.active:
            raise PermissionError(errno.EACCES, "permission denied")
        return real(target, flags, enforce_mode=enforce_mode)

    monkeypatch.setattr(outcome_archive_active, "open_private_regular", refuse_active)

    report = run(tmp_path, policy())

    _assert_refused(report, store_layout)


def test_an_undecodable_active_file_fails_a_record_cap_run(tmp_path: Path) -> None:
    """A cap cannot be planned when the active file's rows cannot be read.

    The cap bounds the whole retained store, including the rows the writer has
    not sealed. A run that counted an undecodable active file as zero rows would
    claim to meet the cap without knowing the history it holds.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    store_layout = layout(tmp_path)
    write_private(store_layout.active, b"not a cache outcome document\n")

    report = run(tmp_path, policy(max_records=4))

    _assert_refused(report, store_layout)


def test_the_age_rule_separates_the_rows_the_writer_will_drop(
    tmp_path: Path,
) -> None:
    """A cutoff reports the retained rows beside the whole file's figures.

    The writer compacts expired rows on its next idle tick, so they are not
    retained history and a budget must not evict closed history for them. The
    scan still reports the whole file, because the report describes what the
    active file holds until that compaction runs.
    """
    path = tmp_path / "cache-outcomes.jsonl"
    expired = records(NOW - dt.timedelta(days=10), 2)
    kept = records(NOW, 3)
    write_private(path, document((*expired, *kept)))

    history = active_history(path, cutoff=NOW - dt.timedelta(days=1))

    assert history.records == 5, "the whole file is still counted"
    assert history.bytes == len(document((*expired, *kept))), (
        "the whole file's bytes are still reported"
    )
    assert history.budget_records == 3, "only the rows the age rule keeps count"
    assert history.budget_bytes == len(document(kept)), (
        "the header and the kept rows are the retained bytes"
    )
    assert history.first_observed_at == NOW - dt.timedelta(days=10), (
        "the span still describes the whole file"
    )


def test_an_append_during_the_scan_is_measured_as_one_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete row appended after the size reading shares its version.

    The writer never takes the archive lock, so it can append between the size
    reading and the end of the scan. The scan then sees the row but holds the
    earlier size; pairing the two would report a byte figure no version of the
    file ever had. The scan is retried against the longer version, so both
    figures describe the same file.
    """
    path = tmp_path / "cache-outcomes.jsonl"
    opened = records(NOW, 2)
    appended = records(NOW, 3)[2]
    write_private(path, document(opened))
    real = outcome_archive_active._size_of
    grew = False

    def grow_after_size(source: typ.BinaryIO, *, fallback: int = 0) -> int:
        """Append one complete row after the first size reading."""
        nonlocal grew
        size = real(source, fallback=fallback)
        if not grew:
            grew = True
            with path.open("ab") as handle:
                handle.write(
                    (outcome_record_encode.encode_line(appended) + "\n").encode()
                )
        return size

    monkeypatch.setattr(outcome_archive_active, "_size_of", grow_after_size)

    history = active_history(path)

    assert history.bytes == len(document(records(NOW, 3))), (
        "the size belongs to the version the rows were read from"
    )
    assert history.records == 3, "the appended row is part of the same version"


def test_a_replaced_active_path_is_measured_from_one_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement between the open and the scan mixes no two versions.

    The size and the rows come from the descriptor that was opened, so a writer
    that moves a compacted file over the path mid-read cannot pair one
    version's bytes with another's rows. Both figures must describe the file
    the scan actually read.
    """
    path = tmp_path / "cache-outcomes.jsonl"
    opened = document(records(NOW, 2))
    write_private(path, opened)
    real = outcome_archive_active.open_private_regular

    def open_then_replace(
        target: Path, flags: int, *, enforce_mode: bool = True
    ) -> tuple[int, os.stat_result]:
        """Open one version, then move a different one over the path."""
        descriptor, metadata = real(target, flags, enforce_mode=enforce_mode)
        if target == path:
            replacement = tmp_path / "replacement.jsonl"
            write_private(replacement, document(records(NOW, 7)))
            replacement.replace(target)
        return descriptor, metadata

    monkeypatch.setattr(
        outcome_archive_active, "open_private_regular", open_then_replace
    )

    history = active_history(path)

    assert history.bytes == len(opened), "the size belongs to the opened version"
    assert history.records == 2, "the rows belong to the same version as the size"
