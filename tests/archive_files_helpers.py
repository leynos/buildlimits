"""Sealed sources, contexts and inventory views shared by the file tests.

Each per-segment archive operation is driven against the real filesystem, so
these helpers write exactly the private files the writer and archiver publish.
A test then states only the operation and the outcome it observes.
"""

import datetime as dt
import typing as typ

from outcome_test_helpers import attributed_record

from buildlimits import outcome_record_encode
from buildlimits.outcome_archive_context import ArchiveContext
from buildlimits.outcome_archive_fs import ensure_private_directory
from buildlimits.outcome_record_format import StoreHeader, StoreRecord
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segment_inventory import SegmentEntry, read_inventory
from buildlimits.outcome_segments import (
    SegmentLayout,
    sealed_meta_name,
    segment_name,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)

__all__ = [
    "NOW",
    "archived_entry",
    "context",
    "document",
    "layout",
    "quarantined",
    "records",
    "sealed_entry",
    "source",
]


def records(count: int, *, first: int = 0) -> tuple[StoreRecord, ...]:
    """Build ``count`` records observed one minute apart.

    Returns
    -------
    tuple[StoreRecord, ...]
        The records, oldest first.
    """
    return tuple(
        attributed_record(observed_at=NOW - dt.timedelta(minutes=index))
        for index in range(first + count - 1, first - 1, -1)
    )


def document(rows: tuple[StoreRecord, ...], *, salt_id: str = "salt-1") -> bytes:
    """Encode a header and records as one JSON Lines document.

    Returns
    -------
    bytes
        The complete, newline-terminated document.
    """
    lines = [outcome_record_encode.encode_line(StoreHeader(salt_id, NOW))]
    lines.extend(outcome_record_encode.encode_line(row) for row in rows)
    return ("\n".join(lines) + "\n").encode()


def source(root: Path, sequence: int, data: bytes) -> Path:
    """Write one private sealed source and its writer metadata.

    Returns
    -------
    Path
        The source path.
    """
    store_layout = layout(root)
    store_layout.sealed_dir.mkdir(parents=True, exist_ok=True)
    path = store_layout.sealed_dir / segment_name(sequence)
    path.write_bytes(data)
    path.chmod(0o600)
    (store_layout.sealed_dir / sealed_meta_name(sequence)).write_text("{}")
    return path


def layout(root: Path) -> SegmentLayout:
    """Return the layout for a store below ``root``.

    Returns
    -------
    SegmentLayout
        The layout shared by the tests.
    """
    return SegmentLayout.beside(root / "cache-outcomes.jsonl")


def context(
    root: Path,
    *,
    preset: int = 0,
    policy: RetentionPolicy | None = None,
) -> ArchiveContext:
    """Build one run context, creating the private archive directory.

    Returns
    -------
    ArchiveContext
        The context the operation needs.
    """
    store_layout = layout(root)
    ensure_private_directory(store_layout.archive_dir)
    return ArchiveContext(
        layout=store_layout,
        policy=RetentionPolicy() if policy is None else policy,
        preset=preset,
        now=NOW,
    )


def sealed_entry(path: Path, sequence: int) -> SegmentEntry:
    """Describe one sealed source as the inventory would.

    Returns
    -------
    SegmentEntry
        The source entry.
    """
    return SegmentEntry(
        sequence=sequence,
        path=path,
        state="sealed",
        physical_bytes=path.stat().st_size,
        logical_bytes=path.stat().st_size,
    )


def archived_entry(root: Path) -> SegmentEntry:
    """Return the inventory's view of the one published archive.

    Returns
    -------
    SegmentEntry
        The archived entry.
    """
    return read_inventory(layout(root)).archived[0]


def quarantined(root: Path) -> list[Path]:
    """List the files a run moved into quarantine.

    Returns
    -------
    list[Path]
        The quarantine entries.
    """
    directory = layout(root).quarantine_dir
    return sorted(directory.iterdir()) if directory.exists() else []
