"""Fixtures shared by the segment naming, state and inventory tests.

The helpers write the same private files the writer and archiver publish, so a
test observes the layout exactly as a later run would find it.
"""

import datetime as dt
import json
import typing as typ

from buildlimits.outcome_segment_sidecar import Sidecar
from buildlimits.outcome_segments import ARCHIVE_DIR_NAME, SegmentLayout, sidecar_name

if typ.TYPE_CHECKING:
    from pathlib import Path

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
DIGEST = "a" * 64

__all__ = [
    "DIGEST",
    "NOW",
    "sidecar_document",
    "store_layout",
    "write_private",
    "write_sidecar",
]


def store_layout(root: Path) -> SegmentLayout:
    """Derive the layout for one store below ``root``.

    Returns
    -------
    SegmentLayout
        The sibling paths the writer and archiver share.
    """
    return SegmentLayout.beside(root / "cache-outcomes.jsonl")


def write_private(path: Path, data: bytes) -> Path:
    """Create one private file, making its parent directory first.

    Returns
    -------
    Path
        The written path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def sidecar_document(sequence: int, **overrides: object) -> dict[str, object]:
    """Build the JSON document for one valid sidecar.

    Returns
    -------
    dict[str, object]
        The sidecar mapping, optionally overridden by the caller.
    """
    document = Sidecar(
        sequence=sequence,
        salt_id="salt-1",
        record_count=2,
        logical_bytes=100,
        physical_bytes=40,
        sha256=DIGEST,
        first_observed_at=NOW - dt.timedelta(minutes=1),
        last_observed_at=NOW,
        archived_at=NOW,
        xz_preset=1,
        had_torn_tail=False,
    ).as_dict()
    document.update(overrides)
    return document


def write_sidecar(root: Path, sequence: int, **overrides: object) -> Path:
    """Write one sidecar document beside a presumed archive.

    Returns
    -------
    Path
        The sidecar path.
    """
    document = sidecar_document(sequence, **overrides)
    return write_private(
        root / ARCHIVE_DIR_NAME / sidecar_name(sequence),
        json.dumps(document).encode(),
    )
