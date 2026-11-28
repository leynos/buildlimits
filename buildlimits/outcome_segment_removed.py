"""Durable evidence that retention removed one sequence on purpose.

The retention floor is installed once a run has completed, so a process that
stops between an unlink and that write leaves no record of why the sequence
went away. A removal marker is published before the unlink and cleared once a
floor covers it, so the gap scan and the next run's floor can both read the
deletion from the store itself.

A marker names a sequence, not the bytes that used to be there, so it stays
small however large the removed segment was. A marker whose sequence is still
present proves nothing: the unlink may have been the step that failed, and the
floor derivation ignores it.
"""

import contextlib
import os
import re
import typing as typ

from buildlimits.outcome_archive_dirs import open_private_chain_when_present
from buildlimits.outcome_archive_fs import (
    atomic_write_json,
    is_proven_absent,
    remove_private_files,
)
from buildlimits.outcome_segments import (
    archive_name,
    removed_marker_name,
    segment_name,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_segments import SegmentLayout

_REMOVED_RE = re.compile(r"^segment-(\d{12})\.jsonl\.removed$")

__all__ = [
    "clear_covered",
    "clear_restored",
    "record_removal",
    "removed_sequences",
    "withdraw_if_survived",
    "withdraw_removal",
]


def record_removal(directory: Path, sequence: int) -> None:
    """Publish the record that retention is removing one sequence.

    The marker is durable before any file is unlinked, so a process that stops
    at any later point leaves an intent the next firing can read.

    Parameters
    ----------
    directory : Path
        The private archive directory the marker lives in.
    sequence : int
        The sequence retention is removing.

    Notes
    -----
    A marker that cannot be written propagates as `OSError` from the atomic
    writer. The caller must not unlink before this succeeds, so the fault
    stops the removal rather than losing the record.
    """
    atomic_write_json(
        directory / removed_marker_name(sequence),
        {"schema": 1, "sequence": sequence},
    )


def removed_sequences(directory: Path) -> frozenset[int]:
    """Return the sequences a removal marker names.

    Parameters
    ----------
    directory : Path
        The private archive directory to scan, which may not exist yet.

    Returns
    -------
    frozenset[int]
        The sequences retention recorded as removed, whoever removed them.

    Notes
    -----
    A directory that cannot be listed propagates ``OSError``: reporting no
    markers for an unreadable store would turn its removals into gaps.
    """
    descriptor = open_private_chain_when_present(directory)
    if descriptor is None:
        return frozenset()
    sequences: set[int] = set()
    try:
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                if match := _REMOVED_RE.fullmatch(entry.name):
                    sequences.add(int(match.group(1)))
    finally:
        os.close(descriptor)
    return frozenset(sequences)


def clear_covered(directory: Path, floor: int) -> None:
    """Remove the markers a recorded floor already explains.

    A marker is only needed until a floor records the deletion; afterwards the
    floor itself says the sequence was removed on purpose. A failure is
    suppressed because the floor is already installed and a leftover marker
    changes no decision the store makes.

    Parameters
    ----------
    directory : Path
        The private archive directory holding the markers.
    floor : int
        The recorded floor. Markers below it are removed.
    """
    try:
        covered = [
            removed_marker_name(sequence)
            for sequence in sorted(removed_sequences(directory))
            if sequence < floor
        ]
        if covered:
            remove_private_files(directory, covered)
    except OSError:
        # The floor is the durable record; a marker left behind is harmless.
        pass


def withdraw_removal(directory: Path, sequence: int) -> None:
    """Withdraw one removal marker whose unlink did not happen.

    A marker published before a failed unlink must not outlive the failure: it
    would explain a later loss of a sequence the store still holds. A failure
    to remove the marker is suppressed because the next maintenance run
    reconciles it again.

    Parameters
    ----------
    directory : Path
        The private archive directory the marker lives in.
    sequence : int
        The sequence whose removal did not complete.
    """
    with contextlib.suppress(OSError):
        # A marker left behind is reconciled by clear_restored.
        remove_private_files(directory, [removed_marker_name(sequence)])


def withdraw_if_survived(directory: Path, sequence: int, path: Path) -> None:
    """Withdraw a marker when the entry survived the unlink that failed.

    An entry the fault removed is proven absent, so its marker stays and the
    deletion is still recorded. An entry that is still there, or whose
    presence cannot be proven either way, withdraws the marker: a surviving
    sequence must not be excused by the record of a removal that did not
    happen.

    Parameters
    ----------
    directory : Path
        The private archive directory the marker lives in.
    sequence : int
        The sequence whose removal failed.
    path : Path
        The entry the removal was unlinking.
    """
    if not is_proven_absent(path):
        withdraw_removal(directory, sequence)


def clear_restored(layout: SegmentLayout) -> None:
    """Withdraw the markers whose sequence the store still holds.

    A process can stop after publishing a marker and before its unlink, and a
    failed unlink leaves the same pair. The next maintenance run reconciles the
    two: a sequence that still has a sealed source or an archive was not
    removed, so its marker must not explain a later loss.

    Parameters
    ----------
    layout : SegmentLayout
        The store whose markers are reconciled.

    Notes
    -----
    A store that cannot be listed propagates ``OSError``; maintenance then
    reports the fault rather than trusting a marker it could not check.
    """
    for sequence in sorted(removed_sequences(layout.archive_dir)):
        held = (
            _is_held(layout.sealed_dir / segment_name(sequence)),
            _is_held(layout.archive_dir / archive_name(sequence)),
        )
        if any(held):
            withdraw_removal(layout.archive_dir, sequence)


def _is_held(path: Path) -> bool:
    """Whether one file is proven to exist.

    ``Path.exists`` answers ``False`` for every ``OSError``, so an entry the
    process may not inspect must not be mistaken for an absent one.

    Returns
    -------
    bool
        True only when the file's metadata could be read.
    """
    try:
        path.stat()
    except OSError:
        return False
    return True
