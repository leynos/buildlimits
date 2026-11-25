"""What a restart must do to the store before the writer may append.

Loading validated rows is only half of startup: the file they came from may
hold a torn tail, may have grown past its bound while the process was stopped,
or may belong to a salt domain that has since been replaced. These rules live
apart from `outcome_store_state` so the transition logic stays readable next to
the file machine it commands.
"""

import dataclasses as dc
import typing as typ

from buildlimits import outcome_records
from buildlimits.outcome_record_format import FORMAT_VERSION
from buildlimits.outcome_store_io import move_aside
from buildlimits.outcome_store_sealing import restart_active, seal
from buildlimits.outcome_store_tail import drop_torn_tail

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_key import Salt
    from buildlimits.outcome_store_state import StoreFiles


def settle_loaded_store(store: StoreFiles) -> None:
    """Install the header, compact, or seal an oversized segment.

    A loaded file whose header is older than the current format is settled
    first, because a current-format row appended beneath it would make the file
    a version 1 document holding version 2 rows: a version 1 reader would then
    report the new row as corrupt rather than the store as a version mismatch.

    Parameters
    ----------
    store : StoreFiles
        The writer's file machine, whose rows are already loaded.
    """
    if not store.policy.is_segmented:
        # The legacy path has one file with no sealed directory, so rewriting
        # the document in place is how it upgrades: compaction always installs
        # a current-format header. A compaction that fails sets
        # ``needs_compaction``, which refuses the next append until a retry
        # succeeds, so no current row is ever written beneath an old header.
        store.compact()
        return
    try:
        drop_torn_tail(store.path)
    except OSError:
        # The decoder already ignores the fragment, but appending would join a
        # new row to it, so a file that cannot be repaired must not be appended
        # to at all. Clearing readiness is what stops appends: a writer that
        # stayed ready would resume above the fragment as soon as the transient
        # fault cleared.
        store.ready = False
        store.publish("load", "save_failed")
        return
    size = store.measure_active()
    if size is None:
        store.publish("load", "unreadable")
        return
    if _settle_existing_file(store, size):
        return
    # Appends never create the active file, so a fresh or emptied store must be
    # given its header at startup. Otherwise the first record fails with
    # ``save_failed`` and is lost, and only the second row, whose retry seals an
    # empty segment, opens the file.
    if size == 0 and not restart_active(store):
        store.publish("load", "save_failed")


def _needs_upgrade(store: StoreFiles) -> bool:
    """Whether the loaded file's header predates the current format.

    Returns
    -------
    bool
        True when a header was loaded and its version is older than the
        version this writer emits.
    """
    header = store.header
    return header is not None and header.version < FORMAT_VERSION


def _settle_existing_file(store: StoreFiles, size: int) -> bool:
    """Apply the restart rule for a segmented file that holds rows.

    An oversized file is sealed before the age and record rules are
    considered, and an age trim is preferred over a seal because compaction
    drops expired rows from the active file directly.

    Parameters
    ----------
    store : StoreFiles
        The writer's file machine, whose rows are already loaded.
    size : int
        The active file's measured size in bytes.

    Returns
    -------
    bool
        Whether the store was sealed or compacted. On ``False`` the caller
        installs the header.
    """
    if size > store.policy.segment_bytes:
        _seal_or_hold_pending(store)
        return True
    if active_expired(store):
        # A restart is the one moment a writer can enforce the age bound on
        # rows that never crossed the segment size. Compaction drops what the
        # cutoff excludes, so the active file cannot hold expired history
        # indefinitely just because it stayed small.
        store.compact()
        return True
    if store.over_record_cap():
        # A reduced cap reaches a segmented store at its next start. Sealing
        # hands the rows to the archiver even though the file is below the
        # segment size, so an idle store cannot hold more rows than the cap
        # merely because it received no further appends.
        _seal_or_hold_pending(store)
        return True
    if _needs_upgrade(store):
        # A small, unexpired active file from an older format would otherwise
        # stay exactly as it is and collect current-format rows under its old
        # header. Sealing closes the old document intact and installs a fresh
        # current one, so no history is dropped and no current row is ever
        # written beneath an old header.
        _settle_old_format(store)
        return True
    return False


def _seal_or_hold_pending(store: StoreFiles) -> None:
    """Seal the active file, keeping pending recovery when the seal failed.

    A seal that renamed the source but could not install a fresh active file
    returns ``False`` with the rows already released. Without a pending mark
    the writer would take the next row and lose it to ``save_failed``, and only
    the append after that would repair the store. The mark makes the next
    append settle first, so the header is recreated before a row is consumed.
    """
    if not seal(store):
        store.needs_compaction = True


def _settle_old_format(store: StoreFiles) -> None:
    """Close an older-format active file before anything is appended to it.

    The in-memory header is upgraded first, so the seal's replacement file is
    current even when the active file held only an old header. A seal that
    fails clears readiness: appending above the old header is exactly what must
    not happen, and a later append would otherwise treat the file as settled.
    """
    header = typ.cast("outcome_records.StoreHeader", store.header)
    store.header = dc.replace(header, version=FORMAT_VERSION)
    if not seal(store):
        store.ready = False
        store.publish("load", "save_failed")


def restore_loaded_store(
    store: StoreFiles,
    decoded: outcome_records.DecodedStore | outcome_records.RecordFailure | None,
    salt: Salt,
    now: dt.datetime,
) -> None:
    """Choose which validated rows can belong to the active salt.

    Parameters
    ----------
    store : StoreFiles
        The writer's file machine, about to take these rows.
    decoded : DecodedStore or RecordFailure or None
        The active file's decoded content, a refusal, or no file at all.
    salt : Salt
        The salt this process activated.
    now : datetime
        The moment used for a replacement header.
    """
    match decoded:
        case outcome_records.RecordFailure() as failure:
            if not move_aside(store.path):
                store.publish("load", failure.reason)
                return
            store.publish("load", failure.reason)
            store.header = outcome_records.StoreHeader(salt.salt_id, now)
            store.set_records([])
        case None:
            store.publish("load", "none")
            store.header = outcome_records.StoreHeader(salt.salt_id, now)
            store.set_records([])
        case outcome_records.DecodedStore() as loaded if (
            loaded.header.salt_id != salt.salt_id
        ):
            if store.policy.is_segmented:
                if not _retire_previous_domain(store, loaded, salt, now):
                    return
                store.publish("load", "salt_changed", records=len(loaded.records))
            else:
                # A legacy store has one file and no sealed directory, so rows
                # keyed under the old salt cannot be kept apart from the new
                # domain and are dropped, as they always were.
                store.publish("load", "salt_changed", discarded=len(loaded.records))
                store.header = outcome_records.StoreHeader(salt.salt_id, now)
                store.set_records([])
        case outcome_records.DecodedStore() as loaded:
            store.publish("load", "none", records=len(loaded.records))
            store.header = loaded.header
            store.set_records(list(loaded.records))
    store.ready = True


def _retire_previous_domain(
    store: StoreFiles,
    loaded: outcome_records.DecodedStore,
    salt: Salt,
    now: dt.datetime,
) -> bool:
    """Preserve rows keyed under a previous salt as a closed segment.

    Appending the new domain's rows to the file that still holds the old
    domain's rows would produce a segment mixing two salts. Sealing first keeps
    the old rows readable under the salt that keyed them.

    Parameters
    ----------
    store : StoreFiles
        The writer's file machine.
    loaded : DecodedStore
        The active file's decoded rows and their own header.
    salt : Salt
        The newly activated salt.
    now : datetime
        The moment used for a replacement header when no rows exist.

    Returns
    -------
    bool
        Whether the old domain is out of the active file. On ``False`` the
        writer must not become ready: appending would mix the domains.
    """
    if not loaded.records:
        # Only the old header is on disk, so replacing it is enough, but it
        # must actually be replaced: leaving it would key the new domain's rows
        # under the previous salt.
        store.header = outcome_records.StoreHeader(salt.salt_id, now)
        store.set_records([])
        return restart_active(store)
    store.header = loaded.header
    store.set_records(list(loaded.records))
    # ``seal`` installs the active salt's header afterwards, so success leaves
    # the writer holding the new domain and no rows.
    return seal(store, salt_id=loaded.header.salt_id)


def active_expired(store: StoreFiles) -> bool:
    """Whether any loaded row is older than the age cutoff.

    Parameters
    ----------
    store : StoreFiles
        The writer's file machine, whose rows are already loaded.

    Returns
    -------
    bool
        True when an age limit applies and the oldest loaded row predates the
        cutoff. A store with no loaded rows never expires.
    """
    if not store.policy.has_age_limit or not store.records:
        return False
    cutoff = store.policy.cutoff(store.clocks.wall())
    if cutoff is None:
        return False
    return min(record.observed_at for record in store.records) < cutoff
