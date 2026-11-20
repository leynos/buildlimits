"""Stores, runs and decoded history shared by the archive end-to-end tests.

The writer produces the sealed segments every archive test starts from, so
these helpers keep the real store-building call in one place. A test then
states only the archive behaviour it observes.
"""

import datetime as dt
import errno
import itertools
import json
import time
import typing as typ

import pytest
from archive_cli_helpers import sealed_measured_source
from outcome_store_helpers import close_writer, segment_writer
from outcome_test_helpers import attributed_record
from segment_test_helpers import document
from segment_test_helpers import store_layout as layout

from buildlimits import outcome_archive_files
from buildlimits.outcome_archive import RunOptions, RunReport, run_archive
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segments import sealed_meta_name, segment_name
from buildlimits.outcome_store_sealing import sealed_metadata

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_archive_context import ActionResult, ArchiveContext
    from buildlimits.outcome_record_format import DecodedStore, StoreRecord
    from buildlimits.outcome_segment_inventory import SegmentEntry
    from buildlimits.outcome_segments import SegmentLayout
    from buildlimits.outcome_store import StoreWriter

# ``StoreWriter.append`` never blocks: a full queue refuses the row and counts
# it as discarded. Under a loaded host the writer's worker can fall behind a
# fixture that appends thousands of rows, so a refused row is offered again
# once the worker has had a moment to drain. The bound is generous for a
# worker that is merely descheduled, and short enough that a row the writer
# can never encode still fails the test promptly.
_APPEND_ATTEMPTS = 200
_APPEND_RETRY_SECONDS = 0.01

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)

__all__ = [
    "NOW",
    "archives",
    "document",
    "layout",
    "observed",
    "policy",
    "records",
    "refuse",
    "run",
    "seal_active",
    "seal_on_each_archive",
    "write",
]


def policy(
    segment_bytes: int = 1,
    *,
    max_age_days: int = 30,
    max_bytes: int = 0,
    max_records: int | None = None,
) -> RetentionPolicy:
    """Build one archiver policy with the bounds a test needs.

    Returns
    -------
    RetentionPolicy
        A policy that seals one record per segment by default. The temporary
        headroom is four segments, which every real store has room for; a test
        that needs a full filesystem refuses the headroom check instead of
        asking for an impossible size.
    """
    return RetentionPolicy(
        max_age=dt.timedelta(days=max_age_days),
        max_bytes=max_bytes,
        max_records=max_records,
        headroom_bytes=max(segment_bytes, 1) * 4,
        segment_bytes=segment_bytes,
    )


def refuse(*_args: object, **_kwargs: object) -> typ.NoReturn:
    """Raise the error an injected filesystem failure reports.

    A test that patches a store operation to fail needs the failure to look
    like the real call's would: an ``OSError`` carrying an errno. Every archive
    fault test shares this one refusal so they fail the same way.

    Raises
    ------
    OSError
        Always, with ``EIO``.
    """
    raise OSError(errno.EIO, "injected failure")


def records(observed: dt.datetime, count: int) -> tuple[StoreRecord, ...]:
    """Build ``count`` records one minute apart from ``observed``.

    Returns
    -------
    tuple[StoreRecord, ...]
        The records, oldest first.
    """
    return tuple(
        attributed_record(observed_at=observed + dt.timedelta(minutes=index))
        for index in range(count)
    )


def write(
    path: Path, rows: tuple[StoreRecord, ...], retention: RetentionPolicy
) -> None:
    """Append records and close the writer, requiring its worker to stop.

    A plain ``close`` returns at its caller's bound whether or not the worker
    has stopped, so an archiver run that follows could see a half-written
    fixture. Every row must also be accepted before the close: a full append
    queue refuses a row rather than waiting, and a fixture that ignored the
    refusal would silently hand the test a shorter history.
    """
    writer = segment_writer(path, retention, NOW)
    try:
        for row in rows:
            _append_or_retry(writer, row)
    finally:
        close_writer(writer)


def seal_active(
    store_layout: SegmentLayout,
    rows: tuple[StoreRecord, ...],
    sequence: int,
) -> None:
    """Close the active file into a sealed source, as the writer's seal does.

    The store is left exactly as the writer leaves it: the active file is
    renamed into the sealed directory and the metadata that describes the rows
    it holds is published beside it. A test can therefore make a seal happen at
    a point inside an archive run without reaching into the archiver.

    Parameters
    ----------
    store_layout : SegmentLayout
        The layout whose active file is sealed.
    rows : tuple[StoreRecord, ...]
        The rows the active file holds.
    sequence : int
        The sequence the new source takes.
    """
    source = store_layout.sealed_dir / segment_name(sequence)
    store_layout.active.rename(source)
    metadata = sealed_metadata(list(rows), sequence, "salt-1", NOW)
    (source.parent / sealed_meta_name(sequence)).write_text(json.dumps(metadata))


def seal_on_each_archive(
    monkeypatch: pytest.MonkeyPatch,
    store: Path,
    last_sequence: int,
) -> None:
    """Seal one fresh source after each source an archive run archives.

    The writer never takes the archive lock, so it can seal while a run works.
    This replacement for ``outcome_archive_files.archive_source`` seals a new
    one-row source after every archive step up to ``last_sequence``, so the
    run meets a new source in each of its bounded follow-up passes.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        The test's patcher, which owns undoing the replacement.
    store : Path
        The ``cache-outcomes.jsonl`` path the sealed sources belong to.
    last_sequence : int
        The highest sequence the fixture seals; later archive steps only
        archive.
    """
    real = outcome_archive_files.archive_source
    created = itertools.count(2)

    def archive_and_seal(context: ArchiveContext, entry: SegmentEntry) -> ActionResult:
        """Archive one source and seal another until the fixture's bound.

        Returns
        -------
        ActionResult
            The action the real archive step recorded.
        """
        result = real(context, entry)
        sequence = next(created)
        if sequence <= last_sequence:
            sealed_measured_source(store, sequence, records(NOW, 1))
        return result

    monkeypatch.setattr(outcome_archive_files, "archive_source", archive_and_seal)


def _append_or_retry(writer: StoreWriter, row: StoreRecord) -> None:
    """Append one row, retrying while the writer reports a full queue.

    A row the writer keeps refusing beyond the retry bound fails the current
    test, because the history it believes it wrote would be missing that row.
    """
    for _attempt in range(_APPEND_ATTEMPTS):
        writer.append(row)
        if not any(
            result.operation == "append" and result.discarded
            for result in writer.results()
        ):
            return
        # The worker owns the queue's head; let it drain before offering the
        # row again.
        time.sleep(_APPEND_RETRY_SECONDS)
    pytest.fail("the writer refused a row beyond its retry bound")


def run(
    root: Path,
    retention: RetentionPolicy,
    *,
    dry_run: bool = False,
) -> RunReport:
    """Run the archiver once at a fixed wall time.

    Returns
    -------
    RunReport
        The run report.
    """
    return run_archive(layout(root), retention, RunOptions(now=NOW, dry_run=dry_run))


def archives(root: Path) -> list[Path]:
    """List the published archives in sequence order.

    Returns
    -------
    list[Path]
        The ``.jsonl.xz`` files.
    """
    directory = layout(root).archive_dir
    return sorted(directory.glob("*.jsonl.xz")) if directory.exists() else []


def observed(history: DecodedStore) -> tuple[dt.datetime, ...]:
    """Return the observation times of decoded history records.

    Returns
    -------
    tuple[datetime, ...]
        Each record's ``observed_at``.
    """
    return tuple(record.observed_at for record in history.records)
