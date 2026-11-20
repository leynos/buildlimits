"""Which closed history the age rule removes or trims.

The age rule is decided from each entry's observation span alone, so it never
needs a content scan and can be planned for an entry whose sidecar is intact
but whose rows are too old to keep. An entry the rule evicts whole is removed
rather than trimmed, and an archive that straddles the cutoff is scheduled for
the prefix trim that drops its expired rows.
"""

import typing as typ

from buildlimits.outcome_archive_trim import Trim

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_segment_inventory import SegmentEntry

__all__ = ["age_trims", "plan_age_step"]


def plan_age_step(
    entry: SegmentEntry, cutoff: dt.datetime, deletes: dict[int, SegmentEntry]
) -> None:
    """Record a whole-segment age deletion when every row is older.

    An entry without a trustworthy sidecar has neither a count nor a span, so
    no age decision can be made from it. The heal step repairs the sidecar,
    and the next firing plans from the span it rebuilt.

    Parameters
    ----------
    entry : SegmentEntry
        The closed entry to consider.
    cutoff : datetime
        The earliest observation the age rule keeps.
    deletes : dict[int, SegmentEntry]
        The deletions planned so far, keyed by sequence. An owned entry is
        added in place.
    """
    if not entry.has_metadata or entry.last_observed_at is None:
        return
    if entry.last_observed_at < cutoff:
        deletes[entry.sequence] = entry


def age_trims(
    candidates: tuple[SegmentEntry, ...],
    cutoff: dt.datetime | None,
    deletes: dict[int, SegmentEntry],
) -> dict[int, Trim]:
    """Record the prefix trim a straddling archive needs.

    A sealed source that straddles the cutoff is left untrimmed here. This run
    archives it, and the follow-up plan applies the trim to the archive it
    becomes, which is the first point at which it can be rewritten.

    Parameters
    ----------
    candidates : tuple[SegmentEntry, ...]
        Every closed entry the plan may act on.
    cutoff : datetime | None
        The earliest observation the age rule keeps, or None when age
        eviction is disabled.
    deletes : dict[int, SegmentEntry]
        Entries already planned for whole deletion, which are never trimmed.

    Returns
    -------
    dict[int, Trim]
        A trim keyed by sequence for every archive that straddles the cutoff.
    """
    if cutoff is None:
        return {}
    trims: dict[int, Trim] = {}
    for entry in candidates:
        if entry.sequence in deletes or not entry.has_metadata:
            continue
        if entry.state != "archived":
            continue
        first = entry.first_observed_at
        if first is not None and first < cutoff:
            trims[entry.sequence] = Trim(before=cutoff)
    return trims
