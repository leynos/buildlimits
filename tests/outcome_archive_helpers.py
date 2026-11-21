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

# The archive fixtures build their rows with the shared record builder; the
# shorter name keeps the dozens of fixture call sites readable.
from outcome_test_helpers import records_at as records
from segment_test_helpers import document, write_private
from segment_test_helpers import store_layout as layout

from buildlimits import outcome_archive_files, outcome_archive_retain
from buildlimits.outcome_archive import RunOptions, RunReport, run_archive
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segments import sealed_meta_name, segment_name
from buildlimits.outcome_store import MAX_APPEND_QUEUE, StoreWriter
from buildlimits.outcome_store_sealing import sealed_metadata

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_archive_context import ActionResult, ArchiveContext
    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_record_format import DecodedStore, StoreRecord
    from buildlimits.outcome_segment_inventory import SegmentEntry
    from buildlimits.outcome_segments import SegmentLayout

# ``StoreWriter.append`` never blocks: a full queue refuses the row and counts
# it as discarded, while the worker publishes discards for rows it could not
# write. Aggregate results cannot say which row a discard belongs to, so a
# fixture must not read one as a refusal of the row it just offered. This
# ledger spends none of the queue's slack: it keeps fewer rows in flight than
# the queue can hold, which makes a refusal impossible, and fails the test on
# any discard rather than re-offering a row that may already be stored.
_QUEUE_HEADROOM = MAX_APPEND_QUEUE - 1
_APPEND_WAIT_SECONDS = 2.0
_APPEND_POLL_SECONDS = 0.005

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
    "seal_five_rows",
    "seal_five_rows_once",
    "seal_on_each_archive",
    "seal_one_row",
    "store_with_archive",
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


class _AppendLedger:
    """Count a fixture's append calls against the writer's published results."""

    def __init__(self, writer: StoreWriter) -> None:
        """Track one writer's answers to the rows offered through it."""
        self._writer = writer
        self._calls = 0
        self._stored = 0
        self._discarded = 0

    def append(self, row: StoreRecord) -> None:
        """Offer one row once the queue can accept it."""
        self._await_outstanding(at_most=_QUEUE_HEADROOM - 1)
        self._calls += 1
        self._writer.append(row)

    def settle(self, expected: int) -> None:
        """Wait until every offered row is answered without a discard.

        Parameters
        ----------
        expected : int
            The number of rows the fixture offered.
        """
        self._await_outstanding(at_most=0)
        if self._stored != expected:
            pytest.fail(f"the writer stored {self._stored} of {expected} fixture rows")

    def _await_outstanding(self, *, at_most: int) -> None:
        """Wait, within the append bound, until few enough rows are unanswered.

        A discard fails the test instead of being retried: the aggregate
        result does not identify its row, and re-offering the row just
        appended would store that row twice while the discarded one stays
        missing.
        """
        deadline = time.monotonic() + _APPEND_WAIT_SECONDS
        waiting = self._drain()
        while waiting > at_most:
            if time.monotonic() >= deadline:
                pytest.fail(f"the writer left {waiting} fixture rows unanswered")
            time.sleep(_APPEND_POLL_SECONDS)
            waiting = self._drain()

    def _drain(self) -> int:
        """Add the writer's published append outcomes to the counters.

        Returns
        -------
        int
            Rows offered through this ledger and not yet answered.
        """
        for result in self._writer.results():
            if result.operation == "append":
                self._stored += result.records
                self._discarded += result.discarded
        if self._discarded:
            pytest.fail(
                f"the writer discarded {self._discarded} fixture rows; "
                "the history would be shorter than the fixture asked for"
            )
        return self._calls - self._stored


def write(
    path: Path, rows: tuple[StoreRecord, ...], retention: RetentionPolicy
) -> None:
    """Append records and close the writer, requiring its worker to stop.

    A plain ``close`` returns at its caller's bound whether or not the worker
    has stopped, so an archiver run that follows could see a half-written
    fixture. Every row must also be stored before the close: a row the writer
    discards would silently hand the test a shorter history. The ledger keeps
    the queue from filling, so a full queue never refuses a row, and it waits
    for each row's outcome instead of deciding from an older row's discard.
    """
    writer = segment_writer(path, retention, NOW)
    ledger = _AppendLedger(writer)
    try:
        for row in rows:
            ledger.append(row)
        ledger.settle(len(rows))
    finally:
        close_writer(writer)


def store_with_archive(root: Path, rows: int) -> tuple[Path, SegmentLayout]:
    """Build a store whose sequence one is archived, with an empty active file.

    Parameters
    ----------
    root : Path
        The directory the store's files live under.
    rows : int
        The number of rows the archived source holds.

    Returns
    -------
    tuple[Path, SegmentLayout]
        The store path and the layout the archiver uses.
    """
    store = root / "cache-outcomes.jsonl"
    write(store, (), policy(segment_bytes=1_000_000))
    sealed_measured_source(store, 1, records(NOW, rows))
    first = run(root, policy(max_age_days=0, max_records=100))
    if first.status != "ok":
        pytest.fail("the fixture archive is published untrimmed")
    return store, layout(root)


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


def seal_five_rows(store_layout: SegmentLayout) -> None:
    """Seal five rows into source two and leave two newer rows active.

    The seal moves the five rows out of the active file into a source, then
    the active file is refilled with two rows the seal did not see. A test can
    therefore seal at a chosen point inside an archive run and leave the store
    in the state the writer leaves it.

    Parameters
    ----------
    store_layout : SegmentLayout
        The store whose active file is sealed and refilled.
    """
    rows = records(NOW, 5)
    write_private(store_layout.active, document(rows))
    seal_active(store_layout, rows, 2)
    write_private(store_layout.active, document(records(NOW, 2)))


def seal_one_row(store_layout: SegmentLayout, sequence: int) -> None:
    """Seal the single row in the active file as source ``sequence``.

    Parameters
    ----------
    store_layout : SegmentLayout
        The store whose active file is sealed.
    sequence : int
        The sequence the new source takes.
    """
    rows = records(NOW, 1)
    write_private(store_layout.active, document(rows))
    seal_active(store_layout, rows, sequence)


def seal_five_rows_once(
    store_layout: SegmentLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seal five rows at the first active read of an archive run.

    The seal moves rows out of the active file into a source the inventory
    read before it does not list, exactly as the writer can while a plan or
    its report reads the store. Replacing the read keeps the interleaving
    point deterministic.

    Parameters
    ----------
    store_layout : SegmentLayout
        The store whose active file is sealed.
    monkeypatch : pytest.MonkeyPatch
        The test's patcher, which owns undoing the replacement.
    """
    real = outcome_archive_retain.active_history
    sealed = False

    def seal_then_read(
        path: Path, *, cutoff: dt.datetime | None = None
    ) -> ActiveHistory:
        """Seal five rows once, then read the active file.

        Returns
        -------
        ActiveHistory
            The active file's history after the seal.
        """
        nonlocal sealed
        if not sealed:
            sealed = True
            seal_five_rows(store_layout)
        return real(path, cutoff=cutoff)

    monkeypatch.setattr(outcome_archive_retain, "active_history", seal_then_read)


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
