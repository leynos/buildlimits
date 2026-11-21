"""Sealed sources, contexts and inventory views shared by the file tests.

Each per-segment archive operation is driven against the real filesystem, so
these helpers write exactly the private files the writer and archiver publish.
A test then states only the operation and the outcome it observes.
"""

import datetime as dt
import typing as typ

from outcome_test_helpers import attributed_record
from segment_test_helpers import document
from segment_test_helpers import store_layout as layout

from buildlimits.outcome_archive_context import ArchiveContext
from buildlimits.outcome_archive_files import archive_source
from buildlimits.outcome_archive_fs import ensure_private_directory
from buildlimits.outcome_archive_quarantine import fresh_quarantine_token
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segment_inventory import SegmentEntry, read_inventory
from buildlimits.outcome_segments import sealed_meta_name, segment_name, sidecar_name

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreRecord

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)

__all__ = [
    "NOW",
    "archived_entry",
    "context",
    "document",
    "duplicate_pair",
    "layout",
    "published",
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
        quarantine_token=fresh_quarantine_token,
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


def published(
    root: Path, count: int = 1, *, preset: int = 0
) -> tuple[ArchiveContext, SegmentEntry]:
    """Archive one sealed source and describe the archive it published.

    A test that starts from a published archive would otherwise repeat the
    same four fixture steps, each of which the archive's own contract depends
    on: the source must be private, the context must own a private archive
    directory, and the entry must be the inventory's view rather than a
    hand-built one.

    Parameters
    ----------
    root : Path
        The directory the store lives under.
    count : int
        How many rows the one sealed source holds.
    preset : int
        The XZ preset the context records.

    Returns
    -------
    tuple[ArchiveContext, SegmentEntry]
        The run context and the inventory's view of the archive it published.
    """
    sealed = source(root, 1, document(records(count)))
    run = context(root, preset=preset)
    archive_source(run, sealed_entry(sealed, 1))
    return run, archived_entry(root)


def duplicate_pair(
    store: Path, count: int = 2, *, drop_sidecar: bool = False
) -> tuple[ArchiveContext, Path, SegmentEntry, bytes]:
    """Archive one source and restore it beside the archive, as a crash can.

    ``archive_source`` releases the source once its archive verifies. A run
    that published the archive but died before releasing the source leaves
    both copies, which is the pair reconciliation settles, so the source is
    written again here. Returning its bytes lets a test compare the two copies
    without rebuilding the document.

    Parameters
    ----------
    store : Path
        The store path the source and archive belong to.
    count : int
        How many rows the restored source holds.
    drop_sidecar : bool
        Whether the archive's sidecar is removed before the source returns,
        modelling a pair whose crash lost the metadata too.

    Returns
    -------
    tuple[ArchiveContext, Path, SegmentEntry, bytes]
        The run context, the restored source, the published entry and the
        source's bytes.
    """
    data = document(records(count))
    sealed = source(store, 1, data)
    run = context(store)
    archive_source(run, sealed_entry(sealed, 1))
    if drop_sidecar:
        (run.layout.archive_dir / sidecar_name(1)).unlink()
    archived = archived_entry(store)
    sealed.write_bytes(data)
    sealed.chmod(0o600)
    return run, sealed, archived, data


def quarantined(root: Path) -> list[Path]:
    """List the files a run moved into quarantine.

    Returns
    -------
    list[Path]
        The quarantine entries.
    """
    directory = layout(root).quarantine_dir
    return sorted(directory.iterdir()) if directory.exists() else []
