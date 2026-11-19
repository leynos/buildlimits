"""Segment layout, sidecar records and read-only inventory for outcome history.

The active file stays where it has always been, beside its salt. Closed
segments live in a private ``cache-outcomes.sealed`` directory, and their XZ
archives and sidecars live in a private ``cache-outcomes.archive`` directory.
Names are zero padded so a directory listing sorts in append order, and every
inventory read is read-only: the writer and archiver own mutation.
"""

import dataclasses as dc
import json
import os
import re
import stat
import typing as typ

from buildlimits.utc_text import optional_utc_moment, optional_utc_text

if typ.TYPE_CHECKING:
    import datetime as dt
    from pathlib import Path

SEALED_DIR_NAME = "cache-outcomes.sealed"
ARCHIVE_DIR_NAME = "cache-outcomes.archive"
SEQUENCE_NAME = "cache-outcomes.seq"
RETENTION_NAME = "retention.json"
LAST_RUN_NAME = "last-run.json"
QUARANTINE_DIR_NAME = "quarantine"
LOCK_NAME = "archive.lock"
SIDECAR_SCHEMA = 1
_SHA256_HEX_LENGTH = 64
_SEQUENCE_WIDTH = 12
_SEALED_RE = re.compile(r"^segment-(\d{12})\.jsonl$")
_ARCHIVE_RE = re.compile(r"^segment-(\d{12})\.jsonl\.xz$")
_SIDECAR_RE = re.compile(r"^segment-(\d{12})\.jsonl\.xz\.json$")
_SEALED_META_RE = re.compile(r"^segment-(\d{12})\.jsonl\.meta\.json$")
_MAX_SEQUENCE_TEXT = 32

__all__ = [
    "ARCHIVE_DIR_NAME",
    "LAST_RUN_NAME",
    "LOCK_NAME",
    "QUARANTINE_DIR_NAME",
    "RETENTION_NAME",
    "SEALED_DIR_NAME",
    "SEQUENCE_NAME",
    "SIDECAR_SCHEMA",
    "Inventory",
    "SegmentEntry",
    "SegmentLayout",
    "Sidecar",
    "archive_name",
    "read_inventory",
    "read_retention_floor",
    "read_sequence",
    "sealed_meta_name",
    "segment_name",
    "sidecar_name",
]


def segment_name(sequence: int) -> str:
    """Return the sealed file name for one sequence.

    Returns
    -------
    str
        A zero-padded ``segment-<sequence>.jsonl`` name.

    Examples
    --------
    >>> segment_name(7)
    'segment-000000000007.jsonl'
    """
    return f"segment-{sequence:0{_SEQUENCE_WIDTH}d}.jsonl"


def archive_name(sequence: int) -> str:
    """Return the XZ archive name for one sequence.

    Returns
    -------
    str
        A zero-padded ``segment-<sequence>.jsonl.xz`` name.
    """
    return f"{segment_name(sequence)}.xz"


def sidecar_name(sequence: int) -> str:
    """Return the archive sidecar name for one sequence.

    Returns
    -------
    str
        A zero-padded ``segment-<sequence>.jsonl.xz.json`` name.
    """
    return f"{archive_name(sequence)}.json"


def sealed_meta_name(sequence: int) -> str:
    """Return the writer's sealed-segment metadata name.

    Returns
    -------
    str
        A zero-padded ``segment-<sequence>.jsonl.meta.json`` name.
    """
    return f"{segment_name(sequence)}.meta.json"


@dc.dataclass(frozen=True, slots=True)
class SegmentLayout:
    """The paths that make up one outcome store's segment layout.

    Attributes
    ----------
    active : Path
        The supervisor writer's append-only JSON Lines file.
    sealed_dir : Path
        Private directory holding closed, uncompressed segments.
    archive_dir : Path
        Private directory holding XZ archives, sidecars and archiver state.
    sequence_file : Path
        The writer's next-sequence counter.
    retention_file : Path
        The archiver's record of the retention floor.
    last_run_file : Path
        The archiver's most recent run report.
    lock_file : Path
        The archiver's non-blocking exclusive lock.

    Examples
    --------
    >>> from pathlib import Path
    >>> SegmentLayout.beside(Path("/state/cache-outcomes.jsonl")).sealed_dir.name
    'cache-outcomes.sealed'
    """

    active: Path
    sealed_dir: Path
    archive_dir: Path
    sequence_file: Path
    retention_file: Path
    last_run_file: Path
    lock_file: Path

    @property
    def quarantine_dir(self) -> Path:
        """The quarantine directory inside the archive directory.

        Returns
        -------
        Path
            Where unusable segments are preserved without clobbering.
        """
        return self.archive_dir / QUARANTINE_DIR_NAME

    @classmethod
    def beside(cls, active: Path) -> SegmentLayout:
        """Derive the layout from the active store path.

        Parameters
        ----------
        active : Path
            The configured ``cache-outcomes.jsonl`` path.

        Returns
        -------
        SegmentLayout
            Sibling directories and state files.
        """
        root = active.parent
        sealed = root / SEALED_DIR_NAME
        archive = root / ARCHIVE_DIR_NAME
        return cls(
            active=active,
            sealed_dir=sealed,
            archive_dir=archive,
            sequence_file=root / SEQUENCE_NAME,
            retention_file=archive / RETENTION_NAME,
            last_run_file=archive / LAST_RUN_NAME,
            lock_file=archive / LOCK_NAME,
        )


@dc.dataclass(frozen=True, slots=True)
class Sidecar:
    """The verified facts recorded beside one XZ archive.

    Attributes
    ----------
    sequence : int
        The segment sequence.
    salt_id : str
        The record-domain identifier preserved from the source header.
    record_count : int
        Non-header rows in the archive.
    logical_bytes : int
        Uncompressed bytes, including newlines and the header.
    physical_bytes : int
        Bytes the XZ file occupies.
    sha256 : str
        Digest of the uncompressed content.
    first_observed_at : datetime | None
        Earliest row observation, or None when the segment held no rows.
    last_observed_at : datetime | None
        Latest row observation, or None when the segment held no rows.
    archived_at : datetime
        When the archive was published.
    xz_preset : int
        The compression preset used.
    had_torn_tail : bool
        Whether an incomplete final row was dropped.
    """

    sequence: int
    salt_id: str
    record_count: int
    logical_bytes: int
    physical_bytes: int
    sha256: str
    first_observed_at: dt.datetime | None
    last_observed_at: dt.datetime | None
    archived_at: dt.datetime
    xz_preset: int
    had_torn_tail: bool

    def as_dict(self) -> dict[str, object]:
        """Return the stable sidecar JSON fields.

        Returns
        -------
        dict[str, object]
            A JSON-compatible mapping with the schema number.
        """
        return {
            "schema": SIDECAR_SCHEMA,
            "sequence": self.sequence,
            "format": "buildlimits-cache-outcomes",
            "format_version": 1,
            "salt_id": self.salt_id,
            "record_count": self.record_count,
            "logical_bytes": self.logical_bytes,
            "physical_bytes": self.physical_bytes,
            "sha256": self.sha256,
            "first_observed_at": optional_utc_text(self.first_observed_at),
            "last_observed_at": optional_utc_text(self.last_observed_at),
            "archived_at": optional_utc_text(self.archived_at),
            "xz_preset": self.xz_preset,
            "xz_check": "crc64",
            "had_torn_tail": self.had_torn_tail,
        }


@dc.dataclass(frozen=True, slots=True)
class SegmentEntry:
    """One closed segment as the inventory sees it.

    Attributes
    ----------
    sequence : int
        The segment sequence.
    path : Path
        The sealed source or XZ archive.
    state : Literal["sealed", "archived"]
        Which directory owns the entry.
    physical_bytes : int
        Size the file occupies, from ``stat``.
    logical_bytes : int
        Uncompressed bytes represented by the segment.
    record_count : int | None
        Known record count, or None when no metadata describes it.
    first_observed_at : datetime | None
        Earliest row observation, when known.
    last_observed_at : datetime | None
        Latest row observation, when known.
    salt_id : str | None
        Known record domain, when known.
    sha256 : str | None
        Verified content digest, for archives.
    has_metadata : bool
        Whether counts and times came from a trustworthy sidecar.
    """

    sequence: int
    path: Path
    state: typ.Literal["sealed", "archived"]
    physical_bytes: int
    logical_bytes: int
    record_count: int | None = None
    first_observed_at: dt.datetime | None = None
    last_observed_at: dt.datetime | None = None
    salt_id: str | None = None
    sha256: str | None = None
    has_metadata: bool = False


@dc.dataclass(frozen=True, slots=True)
class Inventory:
    """A read-only, sequence-ordered view of closed history.

    Attributes
    ----------
    sealed : tuple[SegmentEntry, ...]
        Sealed but unarchived segments.
    archived : tuple[SegmentEntry, ...]
        Published XZ archives.
    gaps : tuple[int, ...]
        Missing sequences at or above the retention floor.
    quarantined : tuple[int, ...]
        Sequences known to be corrupt or unsupported.
    missing_sidecars : tuple[int, ...]
        Archives with no sidecar.
    corrupt_sidecars : tuple[int, ...]
        Archives whose sidecar cannot be trusted.
    orphan_sidecars : tuple[int, ...]
        Sidecars with no archive.
    retention_floor : int | None
        Lowest sequence intentionally retained, when recorded.
    """

    sealed: tuple[SegmentEntry, ...] = ()
    archived: tuple[SegmentEntry, ...] = ()
    gaps: tuple[int, ...] = ()
    quarantined: tuple[int, ...] = ()
    missing_sidecars: tuple[int, ...] = ()
    corrupt_sidecars: tuple[int, ...] = ()
    orphan_sidecars: tuple[int, ...] = ()
    retention_floor: int | None = None

    @property
    def effective(self) -> tuple[SegmentEntry, ...]:
        """One entry per sequence, preferring a sealed source.

        A sealed source that still exists beside its archive is the authority:
        publication may have crashed before the source was removed, and reading
        the source avoids trusting an archive that was never reconciled.

        Returns
        -------
        tuple[SegmentEntry, ...]
            Entries in ascending sequence order.
        """
        sealed_sequences = {entry.sequence for entry in self.sealed}
        archived = (
            entry for entry in self.archived if entry.sequence not in sealed_sequences
        )
        merged = [*self.sealed, *archived]
        return tuple(sorted(merged, key=lambda entry: entry.sequence))

    @property
    def highest_sequence(self) -> int | None:
        """The highest closed sequence, if any.

        Returns
        -------
        int | None
            The highest sequence, or None when no closed segment exists.
        """
        sequences = [entry.sequence for entry in self.effective]
        return max(sequences) if sequences else None

    @property
    def sealed_bytes(self) -> int:
        """Uncompressed bytes still awaiting archival.

        Returns
        -------
        int
            Summed sealed file sizes.
        """
        return sum(entry.physical_bytes for entry in self.sealed)

    @property
    def archived_logical_bytes(self) -> int:
        """Logical bytes represented by published archives.

        Returns
        -------
        int
            Summed sidecar logical sizes, falling back to physical size when
            metadata is missing.
        """
        return sum(entry.logical_bytes for entry in self.archived)


def read_sequence(path: Path) -> int | None:
    """Read the next-sequence counter without following links.

    Parameters
    ----------
    path : Path
        The counter file.

    Returns
    -------
    int | None
        The stored sequence, or None when the file does not exist.

    Raises
    ------
    ValueError
        If the file is not a regular file, is oversized, or is not a decimal
        integer.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            msg = "sequence counter is not a regular file"
            raise ValueError(msg)
        raw = os.read(descriptor, _MAX_SEQUENCE_TEXT + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_SEQUENCE_TEXT:
        msg = "sequence counter is oversized"
        raise ValueError(msg)
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        msg = "sequence counter is not ASCII"
        raise ValueError(msg) from error
    if not text.isdecimal():
        msg = "sequence counter is not a decimal integer"
        raise ValueError(msg)
    return int(text)


def read_retention_floor(layout: SegmentLayout) -> int | None:
    """Read the retention floor recorded by the last successful run.

    Parameters
    ----------
    layout : SegmentLayout
        The layout whose ``retention.json`` is read.

    Returns
    -------
    int | None
        The lowest retained sequence, or None when no valid state exists.
    """
    try:
        raw = layout.retention_file.read_bytes()
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError, TypeError, RecursionError:
        return None
    if not isinstance(document, dict):
        return None
    floor = document.get("floor_sequence")
    if not isinstance(floor, int) or isinstance(floor, bool):
        return None
    if floor < 0:
        return None
    return floor


def read_inventory(
    layout: SegmentLayout, *, retention_floor: int | None = None
) -> Inventory:
    """Scan closed history without reading segment content.

    Parameters
    ----------
    layout : SegmentLayout
        The layout to scan.
    retention_floor : int | None
        Lowest intentionally retained sequence; sequences below it are not
        reported as gaps.

    Returns
    -------
    Inventory
        Entries in sequence order plus the integrity flags a report shows.
    """
    sealed, sealed_meta = _scan_sealed(layout.sealed_dir)
    archived, missing, corrupt, orphans = _scan_archived(layout.archive_dir)
    entries = tuple(sorted(sealed, key=lambda entry: entry.sequence))
    with_meta = tuple(
        dc.replace(entry, **_metadata_fields(sealed_meta.get(entry.sequence)))
        if entry.sequence in sealed_meta
        else entry
        for entry in entries
    )
    sequences = {entry.sequence for entry in with_meta}
    sequences.update(entry.sequence for entry in archived)
    gaps = _gaps(sequences, retention_floor)
    return Inventory(
        sealed=with_meta,
        archived=tuple(sorted(archived, key=lambda entry: entry.sequence)),
        gaps=gaps,
        missing_sidecars=missing,
        corrupt_sidecars=corrupt,
        orphan_sidecars=orphans,
        retention_floor=retention_floor,
    )


def _scan_sealed(
    directory: Path,
) -> tuple[list[SegmentEntry], dict[int, dict[str, object]]]:
    """List sealed sources and their optional writer metadata.

    Returns
    -------
    tuple[list[SegmentEntry], dict[int, dict[str, object]]]
        Sealed entries and metadata keyed by sequence.
    """
    entries: list[SegmentEntry] = []
    metadata: dict[int, dict[str, object]] = {}
    for name, size in _regular_files(directory):
        matched = _SEALED_RE.fullmatch(name)
        if matched is not None:
            sequence = int(matched.group(1))
            entries.append(
                SegmentEntry(
                    sequence=sequence,
                    path=directory / name,
                    state="sealed",
                    physical_bytes=size,
                    logical_bytes=size,
                )
            )
            continue
        meta_match = _SEALED_META_RE.fullmatch(name)
        if meta_match is not None:
            document = _read_mapping(directory / name)
            if document is not None:
                metadata[int(meta_match.group(1))] = document
    return entries, metadata


def _scan_archived(
    directory: Path,
) -> tuple[list[SegmentEntry], tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """List XZ archives, their sidecars and the orphans beside them.

    Returns
    -------
    tuple[list[SegmentEntry], tuple[int, ...], tuple[int, ...], tuple[int, ...]]
        Entries, sequences with missing sidecars, sequences with corrupt
        sidecars, and sequences whose sidecar has no archive.
    """
    entries: list[SegmentEntry] = []
    archive_sequences: set[int] = set()
    sidecar_sequences: set[int] = set()
    missing: list[int] = []
    corrupt: list[int] = []
    for name, size in _regular_files(directory):
        matched = _ARCHIVE_RE.fullmatch(name)
        if matched is not None:
            sequence = int(matched.group(1))
            archive_sequences.add(sequence)
            sidecar = _read_sidecar(directory / sidecar_name(sequence), sequence)
            if sidecar is None:
                missing.append(sequence)
                entries.append(
                    SegmentEntry(
                        sequence=sequence,
                        path=directory / name,
                        state="archived",
                        physical_bytes=size,
                        logical_bytes=size,
                    )
                )
            elif isinstance(sidecar, Sidecar):
                sidecar_sequences.add(sequence)
                entries.append(
                    SegmentEntry(
                        sequence=sequence,
                        path=directory / name,
                        state="archived",
                        physical_bytes=size,
                        logical_bytes=sidecar.logical_bytes,
                        record_count=sidecar.record_count,
                        first_observed_at=sidecar.first_observed_at,
                        last_observed_at=sidecar.last_observed_at,
                        salt_id=sidecar.salt_id,
                        sha256=sidecar.sha256,
                        has_metadata=True,
                    )
                )
            else:
                corrupt.append(sequence)
                entries.append(
                    SegmentEntry(
                        sequence=sequence,
                        path=directory / name,
                        state="archived",
                        physical_bytes=size,
                        logical_bytes=size,
                    )
                )
            continue
        side_match = _SIDECAR_RE.fullmatch(name)
        if side_match is not None:
            sidecar_sequences.add(int(side_match.group(1)))
    orphans = tuple(sorted(sidecar_sequences - archive_sequences))
    return entries, tuple(sorted(missing)), tuple(sorted(corrupt)), orphans


def _regular_files(directory: Path) -> list[tuple[str, int]]:
    """List regular files in one directory without following links.

    Returns
    -------
    list[tuple[str, int]]
        Name and size of every regular file, or an empty list when the
        directory does not exist.
    """
    try:
        iterator = os.scandir(directory)
    except OSError:
        return []
    found: list[tuple[str, int]] = []
    try:
        for entry in iterator:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                found.append((entry.name, metadata.st_size))
    finally:
        iterator.close()
    return found


def _read_mapping(path: Path) -> dict[str, object] | None:
    """Read a bounded JSON object, or None when it is absent or invalid.

    Returns
    -------
    dict[str, object] | None
        The mapping, or None.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError, TypeError, RecursionError:
        return None
    return document if isinstance(document, dict) else None


def _read_sidecar(path: Path, sequence: int) -> Sidecar | typ.Literal["corrupt"] | None:
    """Read and validate one sidecar, distinguishing absent from corrupt.

    Returns
    -------
    Sidecar | Literal["corrupt"] | None
        The sidecar, the corruption marker, or None when it is absent.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return "corrupt"
    try:
        document = json.loads(raw)
    except ValueError, TypeError, RecursionError:
        return "corrupt"
    if not isinstance(document, dict):
        return "corrupt"
    return _sidecar_from_mapping(document, sequence)


def _sidecar_from_mapping(
    document: dict[str, object], sequence: int
) -> Sidecar | typ.Literal["corrupt"]:
    """Validate one sidecar mapping against its sequence.

    Returns
    -------
    Sidecar | Literal["corrupt"]
        The typed sidecar or the corruption marker.
    """
    try:
        if document.get("schema") != SIDECAR_SCHEMA:
            return "corrupt"
        if document.get("sequence") != sequence:
            return "corrupt"
        archived_at = optional_utc_moment(
            document.get("archived_at"),
            label="sidecar timestamp",
            message="a sidecar timestamp must be text",
        )
        if archived_at is None:
            return "corrupt"
        sha256 = _required_text(document, "sha256")
        is_digest = len(sha256) == _SHA256_HEX_LENGTH and all(
            char in "0123456789abcdef" for char in sha256
        )
        if not is_digest:
            return "corrupt"
        return Sidecar(
            sequence=sequence,
            salt_id=_required_text(document, "salt_id"),
            record_count=_required_int(document, "record_count"),
            logical_bytes=_required_int(document, "logical_bytes"),
            physical_bytes=_required_int(document, "physical_bytes"),
            sha256=_required_text(document, "sha256"),
            first_observed_at=_optional_moment(document, "first_observed_at"),
            last_observed_at=_optional_moment(document, "last_observed_at"),
            archived_at=archived_at,
            xz_preset=_required_int(document, "xz_preset"),
            had_torn_tail=document.get("had_torn_tail") is True,
        )
    except TypeError, ValueError:
        return "corrupt"


def _required_text(document: dict[str, object], name: str) -> str:
    """Read one required non-empty string field.

    Parameters
    ----------
    document : dict[str, object]
        The mapping to read.
    name : str
        The field name.

    Returns
    -------
    str
        The field's non-empty text.

    Raises
    ------
    TypeError
        If the field is not text.
    ValueError
        If the field is missing or empty.
    """
    value = document.get(name)
    if not isinstance(value, str):
        raise TypeError(name)
    if not value.strip():
        raise ValueError(name)
    return value


def _required_int(document: dict[str, object], name: str) -> int:
    """Read one required non-negative integer field.

    Parameters
    ----------
    document : dict[str, object]
        The mapping to read.
    name : str
        The field name.

    Returns
    -------
    int
        The field's non-negative integer value.

    Raises
    ------
    TypeError
        If the field is missing, boolean or not an integer.
    ValueError
        If the field is negative.
    """
    value = document.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(name)
    if value < 0:
        raise ValueError(name)
    return value


def _optional_moment(document: dict[str, object], name: str) -> dt.datetime | None:
    """Read one optional timestamp field.

    Parameters
    ----------
    document : dict[str, object]
        The mapping to read.
    name : str
        The field name.

    Returns
    -------
    datetime | None
        The parsed UTC moment, or None when the field is absent.
    """
    return optional_utc_moment(
        document.get(name),
        label=name,
        message=f"{name} must be text",
    )


def _metadata_fields(
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    """Translate one writer metadata mapping into entry fields.

    Returns
    -------
    dict[str, object]
        Fields to merge into a sealed entry; empty when metadata is unusable.
    """
    if metadata is None:
        return {}
    try:
        return {
            "record_count": _required_int(metadata, "record_count"),
            "first_observed_at": _optional_moment(metadata, "first_observed_at"),
            "last_observed_at": _optional_moment(metadata, "last_observed_at"),
            "salt_id": _required_text(metadata, "salt_id"),
            "has_metadata": True,
        }
    except TypeError, ValueError:
        return {}


def _gaps(sequences: set[int], retention_floor: int | None) -> tuple[int, ...]:
    """Return missing sequences at or above the retention floor.

    Returns
    -------
    tuple[int, ...]
        Missing sequences in ascending order.

    Examples
    --------
    >>> _gaps({2, 4}, 2)
    (3,)
    >>> _gaps({3, 4}, 3)
    ()
    """
    if not sequences:
        return ()
    lowest = 1 if retention_floor is None else max(1, retention_floor)
    highest = max(sequences)
    return tuple(
        sequence for sequence in range(lowest, highest + 1) if sequence not in sequences
    )
