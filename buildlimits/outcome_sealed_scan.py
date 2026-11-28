"""List sealed sources and report which rewrites are unfinished.

A scan here is content-free: it lists regular files through a validated
directory descriptor and trusts the writer's metadata unless an unfinished
in-place rewrite makes that metadata describe bytes that may no longer exist.
"""

import re
import typing as typ

from buildlimits.outcome_segment_scan import (
    SegmentEntry,
    _sequence_set,
    regular_files,
)
from buildlimits.outcome_segment_sidecar import read_mapping

if typ.TYPE_CHECKING:
    from pathlib import Path

_SEALED_RE = re.compile(r"^segment-(\d{12})\.jsonl$")
_SEALED_META_RE = re.compile(r"^segment-(\d{12})\.jsonl\.meta\.json$")
_SEALED_REPLACING_RE = re.compile(r"^segment-(\d{12})\.jsonl\.trimming$")

__all__ = ["scan_sealed"]


def scan_sealed(
    directory: Path,
) -> tuple[list[SegmentEntry], dict[int, dict[str, object]], tuple[int, ...]]:
    """List sealed sources, their optional writer metadata and unfinished work.

    A marked source is listed but its metadata is withheld: the file may
    already have been replaced, so the counts, span and salt it describes may
    belong to bytes that no longer exist. Repair re-derives them.

    Returns
    -------
    tuple[list[SegmentEntry], dict[int, dict[str, object]], tuple[int, ...]]
        Sealed entries, metadata keyed by sequence with every marked source
        omitted, and the sequences a rewrite has not finished, including a
        marker whose source was already removed.
    """
    files = regular_files(directory)
    marked = _marked_sequences(files)
    entries, metadata = _sealed_documents(files, marked)
    return entries, metadata, tuple(sorted(marked))


def _marked_sequences(files: list[tuple[Path, int]]) -> set[int]:
    """Return the sequences named by a sealed rewrite marker.

    Returns
    -------
    set[int]
        Sequences whose in-place rewrite is unfinished.
    """
    return _sequence_set(files, _SEALED_REPLACING_RE)


def _sealed_documents(
    files: list[tuple[Path, int]], marked: set[int]
) -> tuple[list[SegmentEntry], dict[int, dict[str, object]]]:
    """Describe every sealed source and the metadata a scan may trust.

    Returns
    -------
    tuple[list[SegmentEntry], dict[int, dict[str, object]]]
        Sealed entries and metadata keyed by sequence; marked sources are
        withheld.
    """
    entries: list[SegmentEntry] = []
    metadata: dict[int, dict[str, object]] = {}
    for path, size in files:
        matched = _SEALED_RE.fullmatch(path.name)
        if matched is not None:
            entries.append(_sealed_entry(path, size, int(matched.group(1))))
            continue
        meta_match = _SEALED_META_RE.fullmatch(path.name)
        if meta_match is not None and int(meta_match.group(1)) not in marked:
            document = read_mapping(path)
            if document is not None:
                metadata[int(meta_match.group(1))] = document
    return entries, metadata


def _sealed_entry(path: Path, size: int, sequence: int) -> SegmentEntry:
    """Describe one plain sealed source.

    Returns
    -------
    SegmentEntry
        The entry; its logical size is its on-disk size.
    """
    return SegmentEntry(
        sequence=sequence,
        path=path,
        state="sealed",
        physical_bytes=size,
        logical_bytes=size,
    )
