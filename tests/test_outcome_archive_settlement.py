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

from archive_cli_helpers import sealed_measured_source
from outcome_archive_helpers import (
    NOW,
    archives,
    layout,
    policy,
    records,
    run,
    seal_active,
    seal_on_each_archive,
    write,
)
from segment_test_helpers import document, write_private

from buildlimits import outcome_archive_files, outcome_archive_retain
from buildlimits.outcome_archive_report import EXIT_TEMPFAIL
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segments import sidecar_name

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_archive_context import ActionResult, ArchiveContext
    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_archive_trim import Trim
    from buildlimits.outcome_segment_inventory import SegmentEntry
    from buildlimits.outcome_segments import SegmentLayout


def _store_with_archive(root: Path, rows: int) -> tuple[Path, SegmentLayout]:
    """Build a store whose sequence one is archived, with an empty active file.

    Returns
    -------
    tuple[Path, SegmentLayout]
        The store path and the layout the archiver uses.
    """
    store = root / "cache-outcomes.jsonl"
    write(store, (), policy(segment_bytes=1_000_000))
    sealed_measured_source(store, 1, records(NOW, rows))
    first = run(root, policy(max_age_days=0, max_records=100))
    assert first.status == "ok", "the fixture archive is published untrimmed"
    return store, layout(root)


def test_a_trim_the_active_file_grows_past_is_applied_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same trim on a re-grown store is a new decision, not a repeat."""
    store_layout = _store_with_archive(tmp_path, 10)[1]
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
    _, store_layout = _store_with_archive(tmp_path, 1)
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
    store, store_layout = _store_with_archive(tmp_path, 5)
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


def test_the_final_report_retries_a_seal_at_its_active_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seal during the report's active read is counted, not undercounted.

    A seal between the report's inventory scan and its active read moves rows
    out of the active file into a source that scan does not list, so the pair
    would count neither copy. The report re-reads the store and describes one
    version, so the sealed rows appear in the count and the span.
    """
    _, store_layout = _store_with_archive(tmp_path, 1)
    real = outcome_archive_retain.active_history
    sealed = False

    def seal_at_report_read(
        path: Path, *, cutoff: dt.datetime | None = None
    ) -> ActiveHistory:
        """Seal five rows once, at the report's whole-file read."""
        nonlocal sealed
        if cutoff is None and not sealed:
            sealed = True
            rows = records(NOW, 5)
            write_private(store_layout.active, document(rows))
            seal_active(store_layout, rows, 2)
            write_private(store_layout.active, document(records(NOW, 2)))
        return real(path, cutoff=cutoff)

    monkeypatch.setattr(outcome_archive_retain, "active_history", seal_at_report_read)

    report = run(tmp_path, policy(segment_bytes=1_000_000, max_age_days=30))

    assert report.status == "ok", "the retried reading describes one version"
    assert report.retained_records == 8, (
        "the archive, the sealed source and the new active rows all count"
    )
    assert report.observed_until == NOW + dt.timedelta(minutes=4), (
        "the span reaches the newest row in the sealed source"
    )


def test_a_writer_that_keeps_sealing_leaves_the_report_unsettled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report that cannot bind rows to one version is retryable.

    A writer that seals on every reading never lets the report settle. The run
    must report that as retryable rather than recording one of the partial
    readings as a complete report.
    """
    _, store_layout = _store_with_archive(tmp_path, 1)
    real = outcome_archive_retain.active_history
    sequences = itertools.count(2)

    def seal_on_every_read(
        path: Path, *, cutoff: dt.datetime | None = None
    ) -> ActiveHistory:
        """Seal one fresh source at every whole-file read."""
        if cutoff is None:
            sequence = next(sequences)
            rows = records(NOW, 1)
            write_private(store_layout.active, document(rows))
            seal_active(store_layout, rows, sequence)
        return real(path, cutoff=cutoff)

    monkeypatch.setattr(outcome_archive_retain, "active_history", seal_on_every_read)

    report = run(tmp_path, policy(segment_bytes=1_000_000, max_age_days=30))

    assert report.status == "unsettled", "no reading describes the whole store"
    assert report.exit_status == EXIT_TEMPFAIL, "the timer retries the run"
