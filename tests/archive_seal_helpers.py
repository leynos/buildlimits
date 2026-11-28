"""Seals a test performs at a chosen point inside an archive run.

The writer owns sealing, but a test needs the store to change while a run is
working. These helpers leave the store in exactly the state the writer leaves
it: the active file renamed into the sealed directory beside the metadata that
describes its rows. A test can therefore seal at a point an archive run will
meet, without reaching into the archiver.
"""

import itertools
import json
import typing as typ

from archive_cli_helpers import sealed_measured_source
from outcome_archive_helpers import NOW, records
from segment_test_helpers import document, write_private

from buildlimits import outcome_archive_files, outcome_archive_retain
from buildlimits.outcome_segments import sealed_meta_name, segment_name
from buildlimits.outcome_store_documents import sealed_metadata

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    import pytest

    from buildlimits.outcome_archive_context import ActionResult, ArchiveContext
    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_segment_inventory import SegmentEntry
    from buildlimits.outcome_segments import SegmentLayout

__all__ = [
    "seal_active",
    "seal_five_rows",
    "seal_five_rows_once",
    "seal_on_each_archive",
    "seal_one_row",
]


def seal_active(
    store_layout: SegmentLayout,
    rows: tuple[StoreRecord, ...],
    sequence: int,
) -> None:
    """Close the active file into a sealed source, as the writer's seal does.

    Parameters
    ----------
    store_layout : SegmentLayout
        The layout whose active file is sealed.
    rows : tuple[StoreRecord, ...]
        The rows the active file holds.
    sequence : int
        The sequence the new source takes.
    """
    source = store_layout.sealed_dir / segment_name(sequence)
    store_layout.active.rename(source)
    metadata = sealed_metadata(list(rows), sequence, "salt-1", NOW)
    (source.parent / sealed_meta_name(sequence)).write_text(json.dumps(metadata))


def seal_on_each_archive(
    monkeypatch: pytest.MonkeyPatch,
    store: Path,
    last_sequence: int,
) -> None:
    """Seal one fresh source after each source an archive run archives.

    The writer never takes the archive lock, so it can seal while a run works.
    This replacement for ``outcome_archive_files.archive_source`` seals a new
    one-row source after every archive step up to ``last_sequence``, so the
    run meets a new source in each of its bounded follow-up passes.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        The test's patcher, which owns undoing the replacement.
    store : Path
        The ``cache-outcomes.jsonl`` path the sealed sources belong to.
    last_sequence : int
        The highest sequence the fixture seals; later archive steps only
        archive.
    """
    real = outcome_archive_files.archive_source
    created = itertools.count(2)

    def archive_and_seal(context: ArchiveContext, entry: SegmentEntry) -> ActionResult:
        """Archive one source and seal another until the fixture's bound.

        Returns
        -------
        ActionResult
            The action the real archive step recorded.
        """
        result = real(context, entry)
        sequence = next(created)
        if sequence <= last_sequence:
            sealed_measured_source(store, sequence, records(NOW, 1))
        return result

    monkeypatch.setattr(outcome_archive_files, "archive_source", archive_and_seal)


def seal_five_rows(store_layout: SegmentLayout) -> None:
    """Seal five rows into source two and leave two newer rows active.

    The seal moves the five rows out of the active file into a source, then
    the active file is refilled with two rows the seal did not see. A test can
    therefore seal at a chosen point inside an archive run and leave the store
    in the state the writer leaves it.

    Parameters
    ----------
    store_layout : SegmentLayout
        The store whose active file is sealed and refilled.
    """
    rows = records(NOW, 5)
    write_private(store_layout.active, document(rows))
    seal_active(store_layout, rows, 2)
    write_private(store_layout.active, document(records(NOW, 2)))


def seal_one_row(store_layout: SegmentLayout, sequence: int) -> None:
    """Seal the single row in the active file as source ``sequence``.

    Parameters
    ----------
    store_layout : SegmentLayout
        The store whose active file is sealed.
    sequence : int
        The sequence the new source takes.
    """
    rows = records(NOW, 1)
    write_private(store_layout.active, document(rows))
    seal_active(store_layout, rows, sequence)


def seal_five_rows_once(
    store_layout: SegmentLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seal five rows at the first active read of an archive run.

    The seal moves rows out of the active file into a source the inventory
    read before it does not list, exactly as the writer can while a plan or
    its report reads the store. Replacing the read keeps the interleaving
    point deterministic.

    Parameters
    ----------
    store_layout : SegmentLayout
        The store whose active file is sealed.
    monkeypatch : pytest.MonkeyPatch
        The test's patcher, which owns undoing the replacement.
    """
    real = outcome_archive_retain.active_history
    sealed = False

    def seal_then_read(
        path: Path,
        *,
        cutoff: dt.datetime | None = None,
        on_retry: cabc.Callable[[str, int, str], None] | None = None,
    ) -> ActiveHistory:
        """Seal five rows once, then read the active file.

        Returns
        -------
        ActiveHistory
            The active file's history after the seal.
        """
        nonlocal sealed
        if not sealed:
            sealed = True
            seal_five_rows(store_layout)
        return real(path, cutoff=cutoff, on_retry=on_retry)

    monkeypatch.setattr(outcome_archive_retain, "active_history", seal_then_read)
