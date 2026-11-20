"""What the archiver accepts as the writer's active history.

The active file is read without the archive lock, so it is a snapshot rather
than a stable input: only an absent file means there is no active history. A
path that is not a regular file, one that cannot be inspected, and a file whose
rows cannot be decoded must not make the store look smaller than it is to the
byte budget or the record cap.
"""

import errno
import typing as typ
from pathlib import Path

import pytest
from outcome_archive_helpers import NOW, document, layout, policy, records, run, write

from buildlimits import outcome_archive_active
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
    real_lstat = Path.lstat

    def refuse_active(self: Path) -> os.stat_result:
        """Fail the inspection of the active path alone."""
        if self == store_layout.active:
            raise PermissionError(errno.EACCES, "permission denied")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", refuse_active)

    report = run(tmp_path, policy())

    _assert_refused(report, store_layout)


def test_an_unreadable_active_file_fails_a_record_cap_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cap cannot be planned when the active file's rows cannot be read.

    The cap bounds the whole retained store, including the rows the writer has
    not sealed. A run that counted an unreadable active file as zero rows would
    claim to meet the cap without knowing the history it holds.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    store_layout = layout(tmp_path)

    def refuse(*_args: object, **_kwargs: object) -> object:
        """Fail the active file's row scan, as an unreadable file would."""
        raise PermissionError(errno.EACCES, "permission denied")

    monkeypatch.setattr(outcome_archive_active, "read_source_facts", refuse)

    report = run(tmp_path, policy(max_records=4))

    _assert_refused(report, store_layout)
