"""Fixtures shared by the segment naming, state and inventory tests.

The helpers write the same private files the writer and archiver publish, so a
test observes the layout exactly as a later run would find it.
"""

import datetime as dt
import json
import stat
import typing as typ

from buildlimits import outcome_record_encode
from buildlimits.outcome_record_format import StoreHeader, StoreRecord
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segment_sidecar import Sidecar
from buildlimits.outcome_segments import ARCHIVE_DIR_NAME, SegmentLayout, sidecar_name

if typ.TYPE_CHECKING:
    from pathlib import Path

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
DIGEST = "a" * 64

__all__ = [
    "DIGEST",
    "NOW",
    "document",
    "history_policy",
    "mode",
    "sidecar_document",
    "store_layout",
    "write_private",
    "write_sidecar",
]


def history_policy(
    segment_bytes: int = 1,
    *,
    max_records: int | None = None,
    max_age_days: int = 30,
) -> RetentionPolicy:
    """Build an unlimited-budget policy that seals at ``segment_bytes``.

    Returns
    -------
    RetentionPolicy
        A policy with no byte eviction, so archived history survives.
    """
    return RetentionPolicy(
        max_age=dt.timedelta(days=max_age_days),
        max_bytes=0,
        max_records=max_records,
        headroom_bytes=max(segment_bytes, 1) * 4,
        segment_bytes=segment_bytes,
    )


def mode(path: Path) -> int:
    """Return a path's permission bits.

    Returns
    -------
    int
        The ``st_mode`` permission bits without the file type.
    """
    return stat.S_IMODE(path.lstat().st_mode)


def document(rows: tuple[StoreRecord, ...], salt_id: str = "salt-1") -> bytes:
    """Encode a header and records as one JSON Lines document.

    Returns
    -------
    bytes
        The complete, newline-terminated document.
    """
    lines = [outcome_record_encode.encode_line(StoreHeader(salt_id, NOW))]
    lines.extend(outcome_record_encode.encode_line(row) for row in rows)
    return ("\n".join(lines) + "\n").encode()


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
