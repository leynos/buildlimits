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

import os
import re
import typing as typ

from buildlimits.outcome_archive_dirs import open_private_chain_when_present
from buildlimits.outcome_archive_fs import atomic_write_json, remove_private_files
from buildlimits.outcome_segments import removed_marker_name

if typ.TYPE_CHECKING:
    from pathlib import Path

_REMOVED_RE = re.compile(r"^segment-(\d{12})\.jsonl\.removed$")

__all__ = [
    "clear_covered",
    "record_removal",
    "removed_sequences",
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
