"""Settling the retention a bounded run leaves behind.

The follow-up passes re-read a store the writer may change without taking the
archive lock. These tests pin the ways a pass can be misled: an active file
that grows after a trim, a seal between the inventory and active reads, and a
quarantine that removes an entry the budget counted. They also pin that the
last permitted pass is confirmed settled rather than assumed.
"""

import datetime as dt
import itertools
import typing as typ
from pathlib import Path

from archive_cli_helpers import sealed_measured_source, sealed_source
from archive_seal_helpers import (
    seal_active,
    seal_five_rows_once,
    seal_on_each_archive,
    seal_one_row,
)
from outcome_archive_helpers import (
    NOW,
    archives,
    layout,
    policy,
    records,
    run,
    store_with_archive,
    write,
)
from segment_test_helpers import document, write_private

from buildlimits import (
    outcome_archive_files,
    outcome_archive_retain,
)
from buildlimits.outcome_archive import inspect_store
from buildlimits.outcome_archive_report import EXIT_TEMPFAIL
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segments import archive_name, sidecar_name

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_archive_context import ActionResult, ArchiveContext
    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_archive_trim import Trim
    from buildlimits.outcome_segment_inventory import SegmentEntry


def test_a_trim_the_active_file_grows_past_is_applied_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same trim on a re-grown store is a new decision, not a repeat."""
    store_layout = store_with_archive(tmp_path, 10)[1]
    real = outcome_archive_files.recompress_archive

    def trim_then_grow(
        context: ArchiveContext, entry: SegmentEntry, trim: Trim
    ) -> ActionResult:
        """Rewrite the archive, then append rows to the active file."""
        result = real(context, entry, trim)
        write_private(store_layout.active, document(records(NOW, 4)))
        return result

    monkeypatch.setattr(outcome_archive_files, "recompress_archive", trim_then_grow)

    report = run(
        tmp_path, policy(segment_bytes=1_000_000, max_age_days=0, max_records=6)
    )

    trimmed = [
        action.sequence for action in report.actions if action.action == "trimmed"
    ]
    assert trimmed == [1, 1], (
        "the archive the active file outgrew is trimmed a second time"
    )
    assert report.retained_records == 6, "the settled store meets the record cap"
    assert report.status == "ok", "a settled run succeeds"


def test_a_seal_between_the_inventory_and_active_reads_is_replanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seal during the follow-up read is seen rather than undercounted."""
    _, store_layout = store_with_archive(tmp_path, 1)
    real = outcome_archive_retain.active_history
    sealed = False

    def seal_then_read(
        path: Path, *, cutoff: dt.datetime | None = None
    ) -> ActiveHistory:
        """Seal five rows once, between the inventory and active reads."""
        nonlocal sealed
        if not sealed:
            sealed = True
            rows = records(NOW, 5)
            write_private(store_layout.active, document(rows))
            seal_active(store_layout, rows, 2)
            write_private(store_layout.active, document(records(NOW, 4)))
        return real(path, cutoff=cutoff)

    monkeypatch.setattr(outcome_archive_retain, "active_history", seal_then_read)

    report = run(
        tmp_path, policy(segment_bytes=1_000_000, max_age_days=0, max_records=5)
    )

    assert not read_inventory(store_layout).sealed, (
        "the source discovered beside the active read is archived"
    )
    assert report.retained_records == 5, "the sealed source counts towards the cap"
    assert report.status == "ok", "the retried pass settles the store"


def test_a_quarantined_source_does_not_evict_older_history(tmp_path: Path) -> None:
    """A source that leaves history invalidates the budget the plan computed."""
    store, store_layout = store_with_archive(tmp_path, 5)
    archive = archives(tmp_path)[0]
    sidecar = store_layout.archive_dir / sidecar_name(1)
    archive_before = archive.read_bytes()
    sidecar_before = sidecar.read_bytes()

    corrupt = sealed_measured_source(store, 2, records(NOW, 10))
    corrupt.write_bytes(b"not a cache outcome document\n")

    report = run(
        tmp_path, policy(segment_bytes=1_000_000, max_age_days=0, max_records=10)
    )

    actions = [(action.action, action.sequence) for action in report.actions]
    assert ("quarantined", 2) in actions, "the corrupt source is quarantined"
    assert ("deleted", 1) not in actions, "the older archive outlives the budget"
    assert archive.read_bytes() == archive_before, "the older archive is untouched"
    assert sidecar.read_bytes() == sidecar_before, "its sidecar still describes it"
    assert report.status == "corrupt", "the quarantine needs an operator"


def test_a_follow_up_quarantine_leaves_the_eviction_unrecorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconcile that quarantines must not credit the pass's eviction.

    ``perform`` returns before its trim and delete steps once a quarantine has
    removed history the plan counted, so none of them ran. Recording them as
    done anyway would make the next pass filter the still-present entry as
    handled and end the run with the budget unmet. The archive here is old
    enough to delete and becomes measured only after the run archives it,
    which is why the first plan could not decide it.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, (), policy(segment_bytes=1_000_000))
    sealed_measured_source(store, 1, records(NOW, 1))
    first = run(tmp_path, policy(segment_bytes=1_000_000, max_age_days=30))
    assert first.status == "ok", "the fixture archive is published"

    sealed_source(store, 2, document(records(NOW, 2)))
    sealed_source(store, 3, document(records(NOW - dt.timedelta(days=2), 2)))
    real = outcome_archive_files.archive_source

    def archive_and_reinstate(
        context: ArchiveContext, entry: SegmentEntry
    ) -> ActionResult:
        """Archive one source, leaving a corrupt copy of source two behind."""
        result = real(context, entry)
        if entry.sequence == 2:
            # A crash between publishing and releasing the source leaves the
            # pair for reconciliation; making the leftover unreadable is what
            # sends it to quarantine without a recorded failure.
            sealed_source(store, 2, b"not a cache outcome document\n")
        return result

    monkeypatch.setattr(outcome_archive_files, "archive_source", archive_and_reinstate)

    report = run(tmp_path, policy(segment_bytes=1_000_000, max_age_days=1))

    actions = [(action.action, action.sequence) for action in report.actions]
    assert ("quarantined", 2) in actions, "the leftover duplicate is quarantined"
    assert ("deleted", 3) in actions, "the expired archive is deleted once settled"
    assert not (layout(tmp_path).archive_dir / archive_name(3)).exists(), (
        "the expired archive does not outlive the run"
    )
    assert report.status == "ok", (
        "a later pass applies the eviction the quarantine held"
    )


def test_the_last_permitted_pass_is_confirmed_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Work finished by the third pass reports ok, not unsettled."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, (), policy(segment_bytes=1_000_000))
    sealed_measured_source(store, 1, records(NOW, 1))
    seal_on_each_archive(monkeypatch, store, 4)

    report = run(tmp_path, policy(max_age_days=0))

    assert not read_inventory(layout(tmp_path)).sealed, "every source is archived"
    assert len(archives(tmp_path)) == 4, "each seal becomes its own archive"
    assert report.status == "ok", "a store settled by the last pass is not retryable"
    assert report.exit_status == 0, "the timer records a successful firing"


def test_inspection_retries_a_seal_at_its_active_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inspection counts the sealed rows instead of a torn inventory pair.

    A seal between inspection's inventory scan and its active read would leave
    the pair counting neither the sealed copy nor the rows that left the active
    file. The retried reading describes one version, so the report and the plan
    agree with the store the inspector was shown.
    """
    _, store_layout = store_with_archive(tmp_path, 1)
    seal_five_rows_once(store_layout, monkeypatch)

    report = inspect_store(
        layout(tmp_path), policy(segment_bytes=1_000_000, max_age_days=30)
    )

    assert report.dry_run, "inspection never changes the store"
    assert report.status == "ok", "the retried reading describes one version"
    assert report.retained_records == 8, (
        "the archive, the sealed source and the active rows all count"
    )
    assert report.observed_until == NOW + dt.timedelta(minutes=4), (
        "the span reaches the newest row in the sealed source"
    )
    assert report.plan is not None, "inspection reports the plan it would run"
    assert report.plan.archive == 1, "the sealed source discovered by the retry"
    assert len(archives(tmp_path)) == 1, "inspection publishes nothing"


def test_a_dry_run_retries_a_seal_at_its_active_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run reports the same settled version inspection reports.

    The report is built from the inventory and active file the plan was
    planned against, so a seal between them cannot make the plan and its
    figures describe different versions of the store.
    """
    _, store_layout = store_with_archive(tmp_path, 1)
    seal_five_rows_once(store_layout, monkeypatch)

    report = run(
        tmp_path, policy(segment_bytes=1_000_000, max_age_days=30), dry_run=True
    )

    assert report.dry_run, "no file was changed"
    assert report.status == "ok", "the retried reading describes one version"
    assert report.retained_records == 8, (
        "the archive, the sealed source and the active rows all count"
    )
    assert report.plan is not None, "a dry run reports the plan it would run"
    assert report.plan.archive == 1, "the sealed source discovered by the retry"


def test_inspection_reports_unsettled_when_no_snapshot_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inspection never reports a plan from a store that keeps moving.

    A writer that seals on every bounded reading leaves no inventory and
    active pair describing one version. Reporting the last pair as a complete
    inspection would undercount history, so inspection reports a retryable
    result and no plan instead.
    """
    _, store_layout = store_with_archive(tmp_path, 1)
    real = outcome_archive_retain.active_history
    sequences = itertools.count(2)

    def seal_on_every_read(
        path: Path, *, cutoff: dt.datetime | None = None
    ) -> ActiveHistory:
        """Seal one fresh source at every active read."""
        seal_one_row(store_layout, next(sequences))
        return real(path, cutoff=cutoff)

    monkeypatch.setattr(outcome_archive_retain, "active_history", seal_on_every_read)

    report = inspect_store(
        layout(tmp_path), policy(segment_bytes=1_000_000, max_age_days=30)
    )

    assert report.status == "unsettled", "no reading describes the whole store"
    assert report.exit_status == EXIT_TEMPFAIL, "the timer retries the run"
    assert report.dry_run, "inspection is still a dry run"
    assert report.plan is None, "no torn pair is reported as a plan"
