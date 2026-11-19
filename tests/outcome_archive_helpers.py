"""Stores, runs and decoded history shared by the archive end-to-end tests.

The writer produces the sealed segments every archive test starts from, so
these helpers keep the real store-building call in one place. A test then
states only the archive behaviour it observes.
"""

import datetime as dt
import typing as typ

from outcome_store_helpers import wait_for_results, wait_for_salt
from outcome_test_helpers import attributed_record

from buildlimits import outcome_record_encode
from buildlimits.outcome_archive import RunOptions, RunReport, run_archive
from buildlimits.outcome_record_format import DecodedStore, StoreHeader, StoreRecord
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segments import SegmentLayout
from buildlimits.outcome_store import StoreWriter

if typ.TYPE_CHECKING:
    from pathlib import Path

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)

__all__ = [
    "NOW",
    "archives",
    "document",
    "layout",
    "observed",
    "policy",
    "ready_writer",
    "records",
    "run",
    "write",
]


def policy(
    segment_bytes: int = 1,
    *,
    max_age_days: int = 30,
    max_bytes: int = 0,
    headroom_bytes: int | None = None,
) -> RetentionPolicy:
    """Build one archiver policy with the bounds a test needs.

    Returns
    -------
    RetentionPolicy
        A policy that seals one record per segment by default.
    """
    return RetentionPolicy(
        max_age=dt.timedelta(days=max_age_days),
        max_bytes=max_bytes,
        max_records=None,
        headroom_bytes=(
            max(segment_bytes, 1) * 4 if headroom_bytes is None else headroom_bytes
        ),
        segment_bytes=segment_bytes,
    )


def ready_writer(path: Path, retention: RetentionPolicy) -> StoreWriter:
    """Start a writer and wait until it accepts appends.

    Returns
    -------
    StoreWriter
        The initialized writer.
    """
    writer = StoreWriter(path, policy=retention, wall_clock=lambda: NOW)
    try:
        wait_for_salt(writer)
        wait_for_results(writer, operation="load")
    except BaseException:
        writer.close(1.0)
        raise
    return writer


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
    """Append records and close the writer, draining every append."""
    writer = ready_writer(path, retention)
    try:
        for row in rows:
            writer.append(row)
    finally:
        writer.close(2.0)


def layout(root: Path) -> SegmentLayout:
    """Return the layout for a store below ``root``.

    Returns
    -------
    SegmentLayout
        The store's segment layout.
    """
    return SegmentLayout.beside(root / "cache-outcomes.jsonl")


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


def document(rows: tuple[StoreRecord, ...], salt_id: str = "salt-1") -> bytes:
    """Encode a header and records as one JSON Lines document.

    Returns
    -------
    bytes
        The complete document.
    """
    lines = [outcome_record_encode.encode_line(StoreHeader(salt_id, NOW))]
    lines.extend(outcome_record_encode.encode_line(row) for row in rows)
    return ("\n".join(lines) + "\n").encode()


def observed(history: DecodedStore) -> tuple[dt.datetime, ...]:
    """Return the observation times of decoded history records.

    Returns
    -------
    tuple[datetime, ...]
        Each record's ``observed_at``.
    """
    return tuple(record.observed_at for record in history.records)
