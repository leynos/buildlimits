"""Planned sizes for individual entries and for the retained store.

Retention decides eviction against estimates, never against the bytes a trim
might free. A sidecar records the logical size and count of the rows an archive
holds, and a planned trim caps the bytes it keeps without claiming to know the
count it frees. The same arithmetic reports what a whole plan retains, so the
budget walks and the report agree on one definition of a surviving entry.
"""

import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_trim import Trim
    from buildlimits.outcome_segment_inventory import Inventory, SegmentEntry

__all__ = ["planned_records", "planned_size", "retained_estimate"]


def planned_records(entry: SegmentEntry, trim: Trim | None) -> int | None:
    """Estimate the records one archive keeps after its trim.

    Returns
    -------
    int | None
        The sidecar's count less a leading count trim, or None when no
        trustworthy sidecar describes the archive. A time or byte trim frees
        no estimated records, because that cannot be known without the rows.
    """
    if entry.record_count is None:
        return None
    if trim is None:
        return entry.record_count
    return max(entry.record_count - trim.records, 0)


def planned_size(entry: SegmentEntry, trim: Trim | None) -> int:
    """Estimate the logical bytes one entry keeps after its trim.

    Returns
    -------
    int
        The recorded logical size, capped by an explicit byte allowance. A
        time-based trim frees nothing here, because that cannot be known
        without reading the rows.
    """
    if trim is None or trim.allowed_bytes is None:
        return entry.logical_bytes
    return min(entry.logical_bytes, trim.allowed_bytes)


def retained_estimate(
    inventory: Inventory,
    *,
    active_bytes: int,
    deletes: tuple[SegmentEntry, ...],
    trims: tuple[tuple[SegmentEntry, Trim], ...],
) -> int:
    """Estimate the logical bytes retained after the planned eviction.

    Returns
    -------
    int
        Active bytes plus every surviving sealed source plus every surviving
        archive, with planned trims applied.
    """
    deleted = {entry.sequence for entry in deletes}
    trim_map = {entry.sequence: trim for entry, trim in trims}
    retained = active_bytes
    for entry in inventory.effective:
        if entry.sequence in deleted:
            continue
        if entry.state == "sealed":
            retained += entry.logical_bytes
        else:
            retained += planned_size(entry, trim_map.get(entry.sequence))
    return retained
