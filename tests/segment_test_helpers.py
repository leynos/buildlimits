"""Fixtures shared by the segment naming, state and inventory tests.

The helpers write the same private files the writer and archiver publish, so a
test observes the layout exactly as a later run would find it.
"""

import datetime as dt
import json
import stat
import typing as typ

from outcome_test_helpers import VERSION_ONE, older_document_row

from buildlimits import outcome_record_encode
from buildlimits.outcome_record_format import StoreHeader, StoreRecord
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segment_sidecar import Sidecar
from buildlimits.outcome_segments import ARCHIVE_DIR_NAME, SegmentLayout, sidecar_name

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
DIGEST = "a" * 64

import typing as typ
__all__ = [
    "DIGEST",
    "NOW",
    "document",
    "history_policy",
    "mode",
    "sidecar_document",
    "store_layout",
    "swap_after_validation",
    "version_one_document",
    "version_two_document",
    "write_private",
    "write_sidecar",
]


def version_one_document(
    rows: tuple[StoreRecord, ...], salt_id: str = "salt-1"
) -> bytes:
    """Encode one header and rows as a version 1 document.

    A version 1 release wrote its header with ``"version": 1`` and its
    outcome rows without the admission and classification fields, so a
    restart fixture must reproduce those bytes to exercise the upgrade rules
    on a document an older release really produced.

    Returns
    -------
    bytes
        The complete, newline-terminated version 1 document.
    """
    header = {
        "format": "buildlimits-cache-outcomes",
        "version": 1,
        "salt_id": salt_id,
        "written_at": "2026-11-19T00:00:00Z",
    }
    lines = [json.dumps(header, separators=(",", ":"))]
    for row in rows:
        record = older_document_row(row, version=VERSION_ONE)
        lines.append(json.dumps(record, separators=(",", ":"), sort_keys=True))
    return ("\n".join(lines) + "\n").encode()


def version_two_document(
    rows: tuple[StoreRecord, ...], salt_id: str = "salt-1"
) -> bytes:
    """Encode one header and rows as a version 2 document.

    A version 2 release wrote the admission and classification evidence but no
    label-time provenance marker, so a fixture that predates that marker omits
    only it. The version 2 fields stay: dropping them would produce a version 1
    row shape under a version 2 header, and a test built on such a fixture
    could not notice that admission time, reason or attribution was lost.

    Returns
    -------
    bytes
        The complete, newline-terminated version 2 document.
    """
    header = {
        "format": "buildlimits-cache-outcomes",
        "version": 2,
        "salt_id": salt_id,
        "written_at": "2026-11-19T00:00:00Z",
    }
    lines = [json.dumps(header, separators=(",", ":"))]
    for row in rows:
        record = json.loads(outcome_record_encode.encode_line(row, version=2))
        for field in _V3_ADDED_FIELDS:
            record.pop(field, None)
        lines.append(json.dumps(record, separators=(",", ":"), sort_keys=True))
    return ("\n".join(lines) + "\n").encode()


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


def swap_after_validation(
    validate: cabc.Callable[[Path], int | None],
    *,
    directory: Path,
    moved: Path,
    elsewhere: Path,
) -> cabc.Callable[[Path], int | None]:
    """Wrap a chain validator so it swaps the directory after validating it.

    A scan or removal that validated a path and then re-opened it would act on
    whatever now occupies that path. The wrapper performs the swap inside the
    validation seam, so a test can prove the operation followed the descriptor
    rather than the path.

    Parameters
    ----------
    validate : collections.abc.Callable[[Path], int | None]
        The chain validator to wrap, typically
        ``open_private_chain_when_present``.
    directory : pathlib.Path
        The directory the wrapper renames once it has been validated.
    moved : pathlib.Path
        Where the validated directory is moved to.
    elsewhere : pathlib.Path
        The directory that a symlink in the validated directory's place points
        to.

    Returns
    -------
    collections.abc.Callable[[Path], int | None]
        A validator with the same contract that swaps ``directory`` before
        returning the descriptor it validated.
    """

    def replace_after_validation(path: Path) -> int | None:
        descriptor = validate(path)
        if descriptor is not None and path == directory:
            directory.rename(moved)
            directory.symlink_to(elsewhere, target_is_directory=True)
        return descriptor

    return replace_after_validation


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
