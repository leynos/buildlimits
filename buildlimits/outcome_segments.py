"""Segment layout and naming for outcome history.

The active file stays where it has always been, beside its salt. Closed
segments live in a private ``cache-outcomes.sealed`` directory, and their XZ
archives and sidecars live in a private ``cache-outcomes.archive`` directory.
Names are zero padded so a directory listing sorts in append order.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

SEALED_DIR_NAME = "cache-outcomes.sealed"
ARCHIVE_DIR_NAME = "cache-outcomes.archive"
SEQUENCE_NAME = "cache-outcomes.seq"
RETENTION_NAME = "retention.json"
PENDING_RETENTION_NAME = "retention-pending.json"
LAST_RUN_NAME = "last-run.json"
LAST_RETAIN_NAME = "last-retain.json"
CLOCK_BASELINE_NAME = "clock-baseline.json"
QUARANTINE_DIR_NAME = "quarantine"
LOCK_NAME = "archive.lock"
SEALED_TEMP_PREFIX = "retain"
"""The prefix a maintenance rewrite gives its temporary files.

The writer creates its own temporaries in the sealed directory while it
publishes metadata, and a concurrent seal must never lose one to the
archiver's cleanup. A dedicated prefix lets a maintenance pass remove only
what it abandoned itself.
"""
_SEQUENCE_WIDTH = 12
MAX_SEQUENCE = 10**_SEQUENCE_WIDTH - 1
"""The largest sequence a twelve-digit segment name can carry."""

__all__ = [
    "ARCHIVE_DIR_NAME",
    "CLOCK_BASELINE_NAME",
    "LAST_RETAIN_NAME",
    "LAST_RUN_NAME",
    "LOCK_NAME",
    "MAX_SEQUENCE",
    "PENDING_RETENTION_NAME",
    "QUARANTINE_DIR_NAME",
    "RETENTION_NAME",
    "SEALED_DIR_NAME",
    "SEALED_TEMP_PREFIX",
    "SEQUENCE_NAME",
    "SegmentLayout",
    "archive_name",
    "maintenance_prefix",
    "removed_marker_name",
    "replacing_name",
    "sealed_marker_name",
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


def _named(sequence: int, suffix: str) -> str:
    """Compose one segment file name from the sequence and a suffix.

    Returns
    -------
    str
        The zero-padded segment name with ``suffix`` appended.
    """
    return f"{segment_name(sequence)}{suffix}"


def maintenance_prefix(sequence: int) -> str:
    """Return the name prefix a maintenance rewrite gives its temporaries.

    The prefix is distinct from the writer's own temporaries, so a concurrent
    seal's metadata file can never be removed by the maintenance cleanup, and
    an abandoned maintenance temporary stays identifiable for the next run.

    Returns
    -------
    str
        The maintenance prefix, carrying the sequence for diagnosis.

    Examples
    --------
    >>> maintenance_prefix(7)
    'retain-segment-000000000007.jsonl'
    """
    return f"{SEALED_TEMP_PREFIX}-{segment_name(sequence)}"


def archive_name(sequence: int) -> str:
    """Return the XZ archive name for one sequence.

    Returns
    -------
    str
        A zero-padded ``segment-<sequence>.jsonl.xz`` name.
    """
    return _named(sequence, ".xz")


def sidecar_name(sequence: int) -> str:
    """Return the archive sidecar name for one sequence.

    Returns
    -------
    str
        A zero-padded ``segment-<sequence>.jsonl.xz.json`` name.
    """
    return _named(sequence, ".xz.json")


def replacing_name(sequence: int) -> str:
    """Return the marker name a rewrite leaves until its sidecar is durable.

    Returns
    -------
    str
        A zero-padded ``segment-<sequence>.jsonl.xz.replacing`` name.

    Examples
    --------
    >>> replacing_name(7)
    'segment-000000000007.jsonl.xz.replacing'
    """
    return _named(sequence, ".xz.replacing")


def sealed_meta_name(sequence: int) -> str:
    """Return the writer's sealed-segment metadata name.

    Returns
    -------
    str
        A zero-padded ``segment-<sequence>.jsonl.meta.json`` name.
    """
    return _named(sequence, ".meta.json")


def removed_marker_name(sequence: int) -> str:
    """Return the marker recording that retention removed one sequence.

    The retention floor is written only after a run completes, so a process
    that stops between an unlink and that write would leave the next firing
    unable to tell an intentional removal from lost history. This marker is
    published before the unlink and cleared once a floor covers it.

    Returns
    -------
    str
        A zero-padded ``segment-<sequence>.jsonl.removed`` name.

    Examples
    --------
    >>> removed_marker_name(7)
    'segment-000000000007.jsonl.removed'
    """
    return _named(sequence, ".removed")


def sealed_marker_name(sequence: int) -> str:
    """Return the marker an in-place sealed rewrite leaves until it is durable.

    A plain sealed source has no sidecar beside it, so the rewrite cannot use
    the archive marker. While this marker exists the source's metadata may
    describe bytes that have already been replaced, and the inventory must
    treat the counts, span and salt as unknown.

    Returns
    -------
    str
        A zero-padded ``segment-<sequence>.jsonl.trimming`` name.

    Examples
    --------
    >>> sealed_marker_name(7)
    'segment-000000000007.jsonl.trimming'
    """
    return _named(sequence, ".trimming")


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
    pending_retention_file : Path
        A floor that could not replace ``retention_file``, read until it can.
    last_run_file : Path
        The archiver's most recent run report.
    last_retain_file : Path
        The retention-only operation's most recent report, kept apart from the
        archive report so a timer can read either without the other's history.
    clock_baseline_file : Path
        The last locked firing's boot identity, wall time and boot time, used
        to withhold age eviction after a clock jump.
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
    pending_retention_file: Path
    last_run_file: Path
    last_retain_file: Path
    clock_baseline_file: Path
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
            pending_retention_file=archive / PENDING_RETENTION_NAME,
            last_run_file=archive / LAST_RUN_NAME,
            last_retain_file=archive / LAST_RETAIN_NAME,
            clock_baseline_file=archive / CLOCK_BASELINE_NAME,
            lock_file=archive / LOCK_NAME,
        )
