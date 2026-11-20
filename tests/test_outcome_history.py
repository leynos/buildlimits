"""Archive-aware history reading.

History spans archived XZ segments, sealed plain segments and the writer's
active file. These tests build a real segmented store with the writer, archive
it with the archiver, and then read it back through the history view.
"""

import dataclasses as dc
import datetime as dt
import lzma
import tracemalloc
import typing as typ
from pathlib import Path

import pytest
from history_fixtures import (
    NOW,
    SALT_NAME,
    active_records,
    archived_segments,
    observed,
    records,
    window,
)
from outcome_archive_helpers import write as write_store
from segment_test_helpers import history_policy

from buildlimits import outcome_history, outcome_replay_cli, outcome_segment_inventory
from buildlimits.outcome_archive import RunOptions, run_archive
from buildlimits.outcome_segments import SegmentLayout, archive_name
from buildlimits.outcome_store_io import read_store

# The first two rows of every two-record fixture, so a fault test states only
# the reason its own injection is expected to produce.
_TWO_ROWS = (NOW, NOW - dt.timedelta(minutes=1))

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import DecodedStore


def test_a_generated_history_archives_and_replays_in_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A multi-segment generated history survives the whole archive-replay path.

    The archive pass is traced so the bounded-memory claim is measured on the
    full path rather than inferred from one segment's benchmark.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    rows = records(0, 6_000)
    policy = history_policy(512 * 1024)
    write_store(store, rows, policy)
    layout = SegmentLayout.beside(store)
    source_bytes = sum(
        path.stat().st_size for path in layout.sealed_dir.glob("*.jsonl")
    )
    assert source_bytes > 3_000_000, "the fixture is large enough to matter"

    tracemalloc.start()
    try:
        report = run_archive(layout, policy, RunOptions(now=NOW))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert report.status == "ok", "the generated history archives cleanly"
    assert len(list(layout.archive_dir.glob("*.jsonl.xz"))) >= 4, (
        "the history spans several segments"
    )
    # The bound is the compressor's own footprint, not the history's: liblzma
    # allocates through Python's allocator, so the traced peak is dominated by
    # the preset's encoder dictionary. It must not grow with the rows read.
    assert peak < 128 * 1024 * 1024, (
        f"archiving {source_bytes} bytes peaked at {peak} traced bytes"
    )

    history = outcome_history.read_history_beside(store)
    assert observed(history.records) == tuple(
        NOW - dt.timedelta(minutes=index) for index in range(6_000)
    ), "every row is read in append order"

    status = outcome_replay_cli.main([
        "h2",
        "--store",
        str(store),
        "--history",
        "--json",
    ])

    assert status == 0, "the archived history replays"
    assert '"attributed_outcomes": 6000' in capsys.readouterr().out, (
        "replay sees every archived row"
    )


def test_history_reads_archived_and_active_records_in_append_order(
    tmp_path: Path,
) -> None:
    """Archived rows and active rows share one append-ordered history."""
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 3)
    active_records(store, first=3, count=2)

    active_only = typ.cast("DecodedStore", read_store(store))
    history = outcome_history.read_history_beside(store)

    assert len(active_only.records) == 2, (
        "the active file alone holds only the newest rows"
    )
    assert observed(history.records) == tuple(
        NOW - dt.timedelta(minutes=index) for index in range(5)
    ), "history reads archived rows before active rows without a gap"


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


def test_all_salts_names_every_domain_it_reads(tmp_path: Path) -> None:
    """A multi-domain history reports the salts its counts span, oldest first."""
    store = tmp_path / "cache-outcomes.jsonl"
    write_store(store, (window(0, 1),), history_policy(1))
    first = typ.cast("DecodedStore", read_store(store)).header.salt_id
    (tmp_path / SALT_NAME).unlink()
    active_records(store, first=2, count=1)
    second = typ.cast("DecodedStore", read_store(store)).header.salt_id
    assert first != second, "the fixture activates a new salt"

    every = outcome_history.read_history_beside(
        store, outcome_history.HistorySelection(all_salts=True)
    )

    assert every.salt_ids == (first, second), "the domains are named oldest first"


def test_history_defaults_to_the_newest_salt_domain(tmp_path: Path) -> None:
    """A salt change starts a new domain; the default reads that domain."""
    store = tmp_path / "cache-outcomes.jsonl"
    policy = history_policy(1)
    write_store(store, (window(0, 1), window(1, 1)), policy)
    (tmp_path / SALT_NAME).unlink()
    active_records(store, first=2, count=2)
    newest_salt = typ.cast("DecodedStore", read_store(store)).header.salt_id

    default = outcome_history.read_history_beside(store)
    selected = outcome_history.read_history_beside(
        store, outcome_history.HistorySelection(salt_id=newest_salt)
    )
    every = outcome_history.read_history_beside(
        store, outcome_history.HistorySelection(all_salts=True)
    )

    assert len(default.records) == 2, "the newest domain is the default"
    assert default.header.salt_id == newest_salt, "the newest header is reported"
    assert observed(default.records) == observed(selected.records), (
        "an explicit newest-salt selection matches the default"
    )
    assert len(every.records) == 4, "every domain is read in append order"


def test_history_reports_an_unknown_salt(tmp_path: Path) -> None:
    """A salt no retained segment carries is a closed fault."""
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 1)

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        outcome_history.read_history_beside(
            store, outcome_history.HistorySelection(salt_id="0" * 64)
        )

    assert raised.value.reason == "unknown_salt", "the reason is closed"


def test_history_reports_a_corrupt_archive(tmp_path: Path) -> None:
    """A damaged archive is a closed fault, not a shortened history."""
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 1)
    archive = next((tmp_path / "cache-outcomes.archive").glob("*.jsonl.xz"))
    archive.write_bytes(b"\x00" * 16)

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        outcome_history.read_history_beside(store)

    assert raised.value.reason in {"bad_xz", "truncated_xz"}, "the fault is bounded"


def test_history_reports_a_missing_store(tmp_path: Path) -> None:
    """A store with no active file and no segments has no history."""
    with pytest.raises(FileNotFoundError):
        outcome_history.read_history_beside(tmp_path / "absent.jsonl")
