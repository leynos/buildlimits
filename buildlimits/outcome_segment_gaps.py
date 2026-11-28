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
    removed: cabc.Iterable[int] = (),
) -> tuple[tuple[int, ...], bool]:
    """Report the sequences missing between the retained entries.

    A sequence retention recorded as removed is not missing history: the floor
    write may not have happened when the process stopped, and the marker is the
    evidence that the removal was intentional.

    Parameters
    ----------
    sealed : Iterable[SegmentEntry]
        Sealed sources the scan listed.
    archived : Iterable[SegmentEntry]
        Published archives the scan listed.
    retention_floor : int | None
        Lowest intentionally retained sequence.
    removed : Iterable[int]
        Sequences whose durable removal markers the scan found.

    Returns
    -------
    tuple[tuple[int, ...], bool]
        The missing sequences oldest first, and whether the list was cut short.
    """
    sequences = {entry.sequence for entry in sealed}
    sequences.update(entry.sequence for entry in archived)
    return gaps_between(sequences, retention_floor, removed=removed)


def gaps_between(
    present: cabc.Iterable[int],
    retention_floor: int | None,
    *,
    removed: cabc.Iterable[int] = (),
) -> tuple[tuple[int, ...], bool]:
    """Return missing sequences at or above the retention floor.

    The walk is linear in the sequences that exist plus the gaps it reports,
    so a pair such as ``1`` and ``1000000000`` costs one tuple rather than a
    billion. The flag says that enumeration stopped at ``MAX_GAP_SEQUENCES``
    and more sequences are missing than are listed.

    A sequence in ``removed`` is retention's own removal, not missing
    history, so it is skipped. Only the survivor sequences bound the walk: a
    removal marker on its own must not extend it past the last entry the store
    still holds, because that is indistinguishable from a store that ended.

    Parameters
    ----------
    present : Iterable[int]
        Sequences the store still holds.
    retention_floor : int | None
        Lowest intentionally retained sequence.
    removed : Iterable[int]
        Sequences a durable removal record explains.

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
    >>> gaps_between({2, 4}, 2, removed={3})
    ((), False)
    >>> len(gaps_between({MAX_GAP_SEQUENCES + 2}, 1)[0])
    4096
    >>> gaps_between({MAX_GAP_SEQUENCES + 2}, 1)[1]
    True
    """
    sequences = set(present)
    if not sequences:
        return (), False
    explained = set(removed)
    lowest = 1 if retention_floor is None else max(1, retention_floor)
    gaps: list[int] = []
    next_missing = lowest
    for present_sequence in sorted(sequences):
        if present_sequence < next_missing:
            continue
        next_missing, truncated = _fill_below(
            present_sequence, next_missing, explained, gaps
        )
        if truncated:
            return tuple(gaps), True
        next_missing = present_sequence + 1
    return tuple(gaps), False


def _fill_below(
    present_sequence: int,
    next_missing: int,
    explained: set[int],
    gaps: list[int],
) -> tuple[int, bool]:
    """Append the unexplained sequences below one present sequence.

    Returns
    -------
    tuple[int, bool]
        The next sequence the caller continues from, and whether the list
        reached ``MAX_GAP_SEQUENCES`` and was cut short.
    """
    while next_missing < present_sequence:
        if next_missing not in explained:
            if len(gaps) == MAX_GAP_SEQUENCES:
                return next_missing, True
            gaps.append(next_missing)
        next_missing += 1
    return next_missing, False
