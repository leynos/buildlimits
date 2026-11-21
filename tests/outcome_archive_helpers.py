"""Stores, runs and decoded history shared by the archive end-to-end tests.

The writer produces the sealed segments every archive test starts from, so
these helpers keep the real store-building call in one place. A test then
states only the archive behaviour it observes.
"""

import datetime as dt
import errno
import typing as typ

import pytest
from append_ledger_helpers import AppendLedger
from archive_cli_helpers import sealed_measured_source
from outcome_store_helpers import close_writer, segment_writer

# The archive fixtures build their rows with the shared record builder; the
# shorter name keeps the dozens of fixture call sites readable.
from outcome_test_helpers import records_at as records
from segment_test_helpers import document
from segment_test_helpers import store_layout as layout

from buildlimits.outcome_archive import RunOptions, RunReport, run_archive
from buildlimits.outcome_retention import RetentionPolicy

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.outcome_record_format import DecodedStore, StoreRecord
    from buildlimits.outcome_segments import SegmentLayout

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


def write(
    path: Path, rows: tuple[StoreRecord, ...], retention: RetentionPolicy
) -> None:
    """Append records and close the writer, requiring its worker to stop.

    A plain ``close`` returns at its caller's bound whether or not the worker
    has stopped, so an archiver run that follows could see a half-written
    fixture. Every row must also be stored before the close: a row the writer
    discards would silently hand the test a shorter history. The ledger keeps
    the queue from filling, so a full queue never refuses a row, and it waits
    for each row's own outcome instead of deciding from an older row's discard.
    """
    writer = segment_writer(path, retention, NOW)
    ledger = AppendLedger(writer)
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


def run(
    root: Path,
    retention: RetentionPolicy,
    *,
    dry_run: bool = False,
    on_retry: cabc.Callable[[str, int, str], None] | None = None,
) -> RunReport:
    """Run the archiver once at a fixed wall time.

    Returns
    -------
    RunReport
        The run report.
    """
    return run_archive(
        layout(root),
        retention,
        RunOptions(now=NOW, dry_run=dry_run, on_retry=on_retry),
    )


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
