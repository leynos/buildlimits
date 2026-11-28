"""Settling a sealed source whose archive was already published.

A crash between publishing an archive and removing its source leaves both
copies. The pair is settled by comparing the archive with the source: a match
removes the redundant source, a mismatch quarantines the archive and either
re-archives the source or, in a retention-only run, keeps it sealed for the
next plan to trim or evict.
"""

import dataclasses as dc
import typing as typ

from buildlimits import outcome_segments
from buildlimits.outcome_archive_context import ActionResult
from buildlimits.outcome_archive_encode import (
    read_archive_facts,
    read_source_facts,
    write_sidecar,
)
from buildlimits.outcome_archive_files import archive_source, release_source
from buildlimits.outcome_archive_fs import remove_private_files
from buildlimits.outcome_archive_quarantine import (
    quarantine_and_remove_sidecar,
    quarantine_source,
)
from buildlimits.outcome_archive_rows import SegmentFaultError

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_segment_inventory import SegmentEntry

__all__ = ["reconcile_duplicate"]


def reconcile_duplicate(
    context: ArchiveContext,
    sealed: SegmentEntry,
    archived: SegmentEntry,
) -> ActionResult:
    """Settle a source whose archive was published before a crash.

    When the archive matches the source, the source is redundant and removed.
    An unreadable or mismatched archive is quarantined, and the source is
    archived again so a later run cannot trust a half-published pair. A corrupt
    source is quarantined and the existing archive is kept.

    A retention-only pass never archives: the unusable archive is quarantined
    and the sealed source is kept, leaving the follow-up plan to trim or evict
    it as sealed history. Quarantining the archive already stops the pass
    before eviction, so the next plan measures the source that remains.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy, moment and operation.
    sealed : SegmentEntry
        The still-present sealed source.
    archived : SegmentEntry
        The archive sharing the source's sequence.

    Returns
    -------
    ActionResult
        Whether the pair was reconciled, re-archived or quarantined.
    """
    rearchive = context.operation != "retain"
    try:
        source = read_source_facts(sealed.path)
    except SegmentFaultError as fault:
        # The archive is intact; only its source is unreadable. Quarantining
        # the source must leave the archive's sidecar, which describes that
        # archive, in place.
        quarantine_source(context, sealed, fault.reason)
        return ActionResult("quarantined", sealed.sequence, reason=fault.reason)
    try:
        existing = read_archive_facts(archived.path)
    except SegmentFaultError as fault:
        quarantine_and_remove_sidecar(
            context, archived.path, archived.sequence, fault.reason
        )
        return _after_quarantine(context, sealed, fault.reason, rearchive=rearchive)
    if not source.matches(existing):
        quarantine_and_remove_sidecar(
            context, archived.path, archived.sequence, "mismatch"
        )
        return _after_quarantine(context, sealed, "mismatch", rearchive=rearchive)
    if not archived.has_metadata:
        # A heal step skips duplicated sequences, so this is the last point at
        # which the verified archive's facts can be recorded before the source
        # that could regenerate them is removed. The sidecar is authoritative.
        write_sidecar(context, archived.sequence, existing)
    release_source(context, sealed)
    return ActionResult(
        "reconciled",
        sealed.sequence,
        existing.logical_bytes,
        archived.physical_bytes,
    )


def _after_quarantine(
    context: ArchiveContext,
    sealed: SegmentEntry,
    reason: str,
    *,
    rearchive: bool,
) -> ActionResult:
    """Decide what follows quarantining a duplicate's unusable archive.

    Returns
    -------
    ActionResult
        The re-archived result, or the quarantine recorded for a pass that may
        not compress.
    """
    if rearchive:
        return _rearchive(context, sealed)
    return ActionResult("quarantined", sealed.sequence, reason=reason)


def _rearchive(context: ArchiveContext, sealed: SegmentEntry) -> ActionResult:
    """Discard a stale archive and archive its source again.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    sealed : SegmentEntry
        The source to archive.

    Returns
    -------
    ActionResult
        The re-archived result.
    """
    remove_private_files(
        context.layout.archive_dir,
        [outcome_segments.sidecar_name(sealed.sequence)],
    )
    result = archive_source(context, sealed)
    return dc.replace(result, action="re-archived")
