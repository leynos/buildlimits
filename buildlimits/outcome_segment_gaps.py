"""Enumerate the sequences missing from the retained history.

A gap list is a diagnostic: it names sequences an operator may need to account
for after a crash. It must stay bounded, because a sparse or damaged numbering
can otherwise make one report cost seconds and gigabytes.
"""

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_segment_inventory import SegmentEntry

__all__ = ["MAX_GAP_SEQUENCES", "gaps_between", "retained_gaps"]

# Past this many gaps the list is cut short and flagged. Without a bound a
# counter that restarts at one leaves no ceiling on one report.
MAX_GAP_SEQUENCES = 4096


def retained_gaps(
    sealed: cabc.Iterable[SegmentEntry],
    archived: cabc.Iterable[SegmentEntry],
    retention_floor: int | None,
) -> tuple[tuple[int, ...], bool]:
    """Report the sequences missing between the retained entries.

    Parameters
    ----------
    sealed : Iterable[SegmentEntry]
        Sealed sources the scan listed.
    archived : Iterable[SegmentEntry]
        Published archives the scan listed.
    retention_floor : int | None
        Lowest intentionally retained sequence.

    Returns
    -------
    tuple[tuple[int, ...], bool]
        The missing sequences oldest first, and whether the list was cut short.
    """
    sequences = {entry.sequence for entry in sealed}
    sequences.update(entry.sequence for entry in archived)
    return gaps_between(sequences, retention_floor)


def gaps_between(
    present: cabc.Iterable[int], retention_floor: int | None
) -> tuple[tuple[int, ...], bool]:
    """Return missing sequences at or above the retention floor.

    The walk is linear in the sequences that exist plus the gaps it reports,
    so a pair such as ``1`` and ``1000000000`` costs one tuple rather than a
    billion. The flag says that enumeration stopped at ``MAX_GAP_SEQUENCES``
    and more sequences are missing than are listed.

    Returns
    -------
    tuple[tuple[int, ...], bool]
        The missing sequences oldest first, and whether the list was cut short.

    Examples
    --------
    >>> gaps_between({2, 4}, 2)
    ((3,), False)
    >>> gaps_between({3, 4}, 3)
    ((), False)
    >>> len(gaps_between({MAX_GAP_SEQUENCES + 2}, 1)[0])
    4096
    >>> gaps_between({MAX_GAP_SEQUENCES + 2}, 1)[1]
    True
    """
    sequences = set(present)
    if not sequences:
        return (), False
    lowest = 1 if retention_floor is None else max(1, retention_floor)
    gaps: list[int] = []
    next_missing = lowest
    for present_sequence in sorted(sequences):
        if present_sequence < next_missing:
            continue
        while next_missing < present_sequence:
            if len(gaps) == MAX_GAP_SEQUENCES:
                return tuple(gaps), True
            gaps.append(next_missing)
            next_missing += 1
        next_missing = present_sequence + 1
    return tuple(gaps), False
