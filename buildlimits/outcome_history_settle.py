"""Detect that the store moved while a history snapshot was being pinned.

A history read must not mix rows from two states of the store. Detection has
two halves: a resolved file's identity changes whenever another process
replaces it, and a fresh scan of the closed segments must still match the one
the pin started from. Both are content-free, so checking them cannot disturb
the store being checked.

``read_inventory`` is reached through its module rather than imported by name,
so a caller that replaces the scan for a test also replaces the one used here.
"""

import typing as typ

from buildlimits import outcome_segment_inventory

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_segment_inventory import Inventory
    from buildlimits.outcome_segments import SegmentLayout

__all__ = ["fingerprint", "is_settled"]


def fingerprint(path: Path) -> tuple[int, int, int]:
    """Return the identity that changes whenever a segment is replaced.

    Returns
    -------
    tuple[int, int, int]
        The file's device, inode and size.
    """
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size


def is_settled(layout: SegmentLayout, inventory: Inventory) -> bool:
    """Whether a fresh scan still lists the same closed segments.

    Returns
    -------
    bool
        True when the closed segment identities and states are unchanged.
    """
    try:
        fresh = outcome_segment_inventory.read_inventory(layout)
    except OSError:
        return False
    return _signature(fresh) == _signature(inventory)


def _signature(inventory: Inventory) -> tuple[tuple[int, str], ...]:
    """Return the closed segment identities and states one scan reported.

    Returns
    -------
    tuple[tuple[int, str], ...]
        One ``(sequence, state)`` pair per effective entry, oldest first.
    """
    return tuple((entry.sequence, entry.state) for entry in inventory.effective)
