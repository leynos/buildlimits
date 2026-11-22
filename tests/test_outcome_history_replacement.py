"""A store that changes under a history read.

History spans archived XZ segments, sealed plain segments and the writer's
active file, and the archiver may replace any of them while a read runs. These
tests build the states such a replacement leaves behind — an archive beside
its surviving source, a source that vanishes after the scan, a seal that lands
mid-read — and check that a read follows the rows instead of shortening the
history or mixing two snapshots.
"""

import dataclasses as dc
import datetime as dt
import lzma
from pathlib import Path

import pytest
from history_fixtures import NOW, archived_segments, observed, records
from outcome_archive_helpers import write as write_store
from segment_test_helpers import history_policy

from buildlimits import outcome_history, outcome_segment_inventory
from buildlimits.outcome_archive import RunOptions, run_archive
from buildlimits.outcome_segments import SegmentLayout, archive_name

# The first two rows of every two-record fixture, so a fault test states only
# the reason its own injection is expected to produce.
_TWO_ROWS = (NOW, NOW - dt.timedelta(minutes=1))


def _archived_store_with_source(
    tmp_path: Path,
) -> tuple[Path, SegmentLayout, Path]:
    """Archive a store and rebuild the sealed source beside its archive.

    The rebuild restores the state a crash can leave behind: an archive
    published while its sealed source still exists, which the next scan lists
    as a duplicate.

    Returns
    -------
    tuple[Path, SegmentLayout, Path]
        The store path, its layout, and the rebuilt sealed source.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    policy = history_policy(1)
    write_store(store, records(0, 2), policy)
    layout = SegmentLayout.beside(store)
    run_archive(layout, policy, RunOptions(now=NOW))
    archive = layout.archive_dir / archive_name(1)
    source = layout.sealed_dir / archive.name.removesuffix(".xz")
    source.write_bytes(lzma.decompress(archive.read_bytes()))
    source.chmod(0o600)
    return store, layout, source


def test_history_reads_a_sealed_source_before_its_archive(
    tmp_path: Path,
) -> None:
    """A sealed source that survived a crash is read instead of its archive."""
    store, _layout, _source = _archived_store_with_source(tmp_path)

    history = outcome_history.read_history_beside(store)

    assert len(history.records) == 2, (
        "reading the surviving sealed source does not duplicate its archived copy"
    )


def test_a_source_removed_after_the_scan_is_read_from_its_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan that lists a source the archiver then removes loses no rows."""
    store, layout, source = _archived_store_with_source(tmp_path)
    scanned = outcome_segment_inventory.read_inventory(layout)
    source.unlink()
    monkeypatch.setattr(
        outcome_segment_inventory, "read_inventory", lambda _layout: scanned
    )

    history = outcome_history.read_history_beside(store)

    assert observed(history.records) == _TWO_ROWS, (
        "the vanished source's rows come from the archive that replaced it"
    )


def _sealed_pair(
    tmp_path: Path,
) -> tuple[Path, SegmentLayout, outcome_segment_inventory.Inventory]:
    """Write two one-record segments and return them with their inventory.

    Returns
    -------
    tuple[Path, SegmentLayout, Inventory]
        The store path, its layout and the scan of the sealed pair.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write_store(store, records(0, 2), history_policy(1))
    layout = SegmentLayout.beside(store)
    complete = outcome_segment_inventory.read_inventory(layout)
    assert len(complete.sealed) == 2, "the fixture seals two segments"
    return store, layout, complete


def test_a_seal_landing_during_the_read_is_rechecked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan that misses a segment sealed mid-read repeats instead of shortening.

    The first scan is made to look as if it ran before the seal: it lists one
    of two closed segments while the active file is already header-only. Only
    the re-scan can recover the rows the seal moved.
    """
    store, _layout, complete = _sealed_pair(tmp_path)
    stale = outcome_segment_inventory.Inventory(sealed=complete.sealed[:1])
    real_scan = outcome_segment_inventory.read_inventory
    scans = 0

    def scan_once_stale(
        target: SegmentLayout, *, retention_floor: int | None = None
    ) -> outcome_segment_inventory.Inventory:
        """Report the pre-seal view first, then the store as it really is."""
        nonlocal scans
        scans += 1
        if scans == 1:
            return stale
        return real_scan(target, retention_floor=retention_floor)

    monkeypatch.setattr(outcome_segment_inventory, "read_inventory", scan_once_stale)

    history = outcome_history.read_history_beside(store)

    assert observed(history.records) == _TWO_ROWS, (
        "the rows the first scan missed are read on the retry"
    )


def test_a_history_that_never_settles_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer that keeps changing the store is reported, never truncated.

    Two scans that disagree forever model a writer sealing faster than the
    reader can confirm the store is still. No pass is complete, so the reader
    must refuse rather than return the rows one partial pass happened to see.
    """
    store, _layout, complete = _sealed_pair(tmp_path)
    partial = dc.replace(complete, sealed=complete.sealed[:1])
    views = [partial, complete]
    scans = 0

    def alternating_scan(
        _target: SegmentLayout, *, retention_floor: int | None = None
    ) -> outcome_segment_inventory.Inventory:
        """Report a different view on every scan so none ever settles."""
        nonlocal scans
        view = views[scans % len(views)]
        scans += 1
        return view

    monkeypatch.setattr(outcome_segment_inventory, "read_inventory", alternating_scan)

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        outcome_history.read_history_beside(store)

    assert raised.value.reason == "unsettled", "the fault is bounded"


def test_a_listed_archive_that_disappeared_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settled scan naming an archive that is gone is a fault, not history."""
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 2)
    layout = SegmentLayout.beside(store)
    scanned = outcome_segment_inventory.read_inventory(layout)
    missing = dc.replace(
        scanned.archived[0], path=scanned.archived[0].path.with_suffix(".gone")
    )
    doctored = dc.replace(scanned, archived=(missing, *scanned.archived[1:]))
    monkeypatch.setattr(
        outcome_segment_inventory,
        "read_inventory",
        lambda _target, **_kwargs: doctored,
    )

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        outcome_history.read_history_beside(store)

    assert raised.value.reason == "vanished", "the fault is bounded"
