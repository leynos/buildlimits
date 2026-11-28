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
    *,
    include_sealed: bool = False,
) -> dict[int, Trim]:
    """Record the prefix trim a straddling entry needs.

    In an archive run a sealed source that straddles the cutoff is left
    untrimmed: the run archives it, and the follow-up plan applies the trim to
    the archive it becomes, which is the first point at which it can be
    rewritten. A retention-only run rewrites the source in place, so the trim
    is planned against the sealed file itself.

    Parameters
    ----------
    candidates : tuple[SegmentEntry, ...]
        Every closed entry the plan may act on.
    cutoff : datetime | None
        The earliest observation the age rule keeps, or None when age
        eviction is disabled.
    deletes : dict[int, SegmentEntry]
        Entries already planned for whole deletion, which are never trimmed.
    include_sealed : bool
        Whether a sealed straddler may be planned for an in-place rewrite.

    Returns
    -------
    dict[int, Trim]
        A trim keyed by sequence for every entry that straddles the cutoff.
    """
    if cutoff is None:
        return {}
    trims: dict[int, Trim] = {}
    for entry in candidates:
        if _straddles(entry, cutoff, deletes=deletes, include_sealed=include_sealed):
            trims[entry.sequence] = Trim(before=cutoff)
    return trims


def _straddles(
    entry: SegmentEntry,
    cutoff: dt.datetime,
    *,
    deletes: dict[int, SegmentEntry],
    include_sealed: bool,
) -> bool:
    """Whether one entry needs a prefix trim for the cutoff.

    Returns
    -------
    bool
        True when the entry survives and holds rows older than the cutoff.
    """
    if entry.sequence in deletes or not entry.has_metadata:
        return False
    if entry.state != "archived" and not include_sealed:
        return False
    first = entry.first_observed_at
    return first is not None and first < cutoff
