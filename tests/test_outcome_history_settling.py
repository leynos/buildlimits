"""How a history read settles, retries and refuses an unusable segment.

A reader repeats its scan so rows sealed mid-read are not missed. The repeat
is only useful if a transient disappearance or an unreadable scan is retried
rather than reported as a shortened history, and a segment with no header must
still be a closed fault.
"""

import collections.abc as cabc
import dataclasses as dc
import errno
import typing as typ
from pathlib import Path

import pytest
from history_fixtures import (
    active_records,
    archived_segments,
    observed,
    records,
    seal_once_after_scan,
    start_domain,
)
from outcome_archive_helpers import write as write_store
from segment_test_helpers import history_policy, write_private

from buildlimits import outcome_history, outcome_records, outcome_segment_inventory
from buildlimits.outcome_segment_inventory import Inventory
from buildlimits.outcome_segments import SegmentLayout, segment_name

if typ.TYPE_CHECKING:
    from buildlimits.outcome_segment_inventory import SegmentEntry

type _Scan = cabc.Callable[[SegmentLayout], Inventory]


def _as_archive(entry: SegmentEntry) -> SegmentEntry:
    """Report a sealed entry as a published archive.

    Parameters
    ----------
    entry : SegmentEntry
        The sealed source the scan listed.

    Returns
    -------
    SegmentEntry
        The entry as it appears once the archiver has replaced it.
    """
    return dc.replace(
        entry,
        path=entry.path.with_name(entry.path.name + ".xz"),
        state="archived",
    )


def _reader(*prepared: SegmentEntry) -> _Scan:
    """Return a scan reporting prepared entries, then the store itself.

    Parameters
    ----------
    prepared : SegmentEntry
        The closed entries each call reports, before the real inventory.

    Returns
    -------
    _Scan
        A scan answering with those entries, then with the real inventory.
    """
    real = outcome_segment_inventory.read_inventory
    remaining = iter(prepared)

    def scan(target: SegmentLayout, *, retention_floor: int | None = None) -> Inventory:
        """Report the next prepared closed entry, or the store itself."""
        entry = next(remaining, None)
        fresh = real(target, retention_floor=retention_floor)
        if entry is None:
            return fresh
        return dc.replace(fresh, sealed=(), archived=(entry,))

    return scan


def test_a_disappearance_during_an_unsettled_scan_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vanished archive is re-read from a later scan, not reported.

    The scans before the read list an archive that is gone while the store is
    still changing, so the read repeats instead of raising ``vanished``.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 2)
    layout = SegmentLayout.beside(store)
    gone = outcome_segment_inventory.read_inventory(layout).archived
    real_inventory = outcome_segment_inventory.read_inventory
    scans = _reader(*map(_as_archive, gone))
    calls = 0

    def settle_on_third(target: SegmentLayout) -> Inventory:
        """Answer with the prepared scans, then as the store really is."""
        nonlocal calls
        calls += 1
        if calls > len(gone):
            return real_inventory(target)
        return scans(target)

    monkeypatch.setattr(outcome_segment_inventory, "read_inventory", settle_on_third)

    history = outcome_history.read_history(layout)

    assert len(history.records) == 2, "every row survives the retry"


def test_an_unreadable_settle_check_never_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settle check that cannot re-scan leaves the store unsettled."""
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 1)
    layout = SegmentLayout.beside(store)
    real = outcome_segment_inventory.read_inventory
    calls = 0

    def alternately_failing(target: SegmentLayout) -> Inventory:
        """Report the store, then fail as a racing scan would."""
        nonlocal calls
        calls += 1
        if calls % 2 == 0:
            raise OSError(errno.EIO, "the scan cannot run")
        return real(target)

    monkeypatch.setattr(
        outcome_segment_inventory, "read_inventory", alternately_failing
    )

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        outcome_history.read_history(layout)

    assert raised.value.reason == "unsettled", "the fault is bounded"
    assert calls == 2 * outcome_history.HISTORY_ATTEMPTS, (
        "every attempt both scanned and failed to settle"
    )


def test_an_empty_scan_is_not_empty_history_once_a_seal_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seal after an empty scan still returns the rows it closed.

    The scan finds no closed segment because the rows are still active, and
    the writer then seals them, so the active path is gone when the emptiness
    check runs. Raising ``FileNotFoundError`` there would hand replay a
    successful empty history while the new segment holds the rows.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    rows = records(0, 1)
    write_store(store, rows, history_policy(10**6))
    layout = SegmentLayout.beside(store)
    seal_once_after_scan(monkeypatch, rows, 1)

    history = outcome_history.read_history(layout)

    assert observed(history.records) == observed(rows), "the sealed rows are read"


def test_a_salt_sealed_during_the_pin_is_not_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A domain that moves into a new segment mid-pin is still retained.

    The scan lists only domain A's archive while domain B's rows are active,
    and the writer seals B before the pin reads the active file. Resolution
    then finds no B segment and no active B header; rejecting the selection
    would deny a domain the store still retains.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 1)
    sealed_b = records(2, 1)
    salt_b = start_domain(store, first=2, count=1)
    seal_once_after_scan(monkeypatch, sealed_b, 2)

    history = outcome_history.read_history_beside(
        store, outcome_history.HistorySelection(salt_id=salt_b)
    )

    assert observed(history.records) == observed(sealed_b), (
        "the domain sealed mid-pin is read from its new segment"
    )


def test_a_segment_with_no_header_is_corrupt(tmp_path: Path) -> None:
    """An empty closed segment is a fault, not an empty document."""
    layout = SegmentLayout.beside(tmp_path / "cache-outcomes.jsonl")
    layout.sealed_dir.mkdir(parents=True)
    write_private(layout.sealed_dir / segment_name(1), b"")

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        outcome_history.read_history(layout)

    assert raised.value.reason == "corrupt", "the fault is closed"


def test_a_segment_replaced_after_the_pin_is_refused(tmp_path: Path) -> None:
    """A replacement between the pre-pass check and the open is a fault.

    Retention can replace a later segment after the pin's pre-pass check has
    passed over it. The identity check on the opened descriptor is what binds
    the rows to the snapshot, so the pass refuses the replacement instead of
    yielding rows from two states of the store.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write_store(store, records(0, 2), history_policy(1))
    layout = SegmentLayout.beside(store)
    history = outcome_history.open_history(layout)
    rows = iter(history)
    next(rows)
    second = layout.sealed_dir / segment_name(2)
    replacement = tmp_path / "replacement"
    write_private(replacement, second.read_bytes())
    replacement.replace(second)

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        list(rows)

    assert raised.value.reason == "unsettled", "the pass refuses a replaced segment"


def test_a_symlinked_active_file_is_corrupt(tmp_path: Path) -> None:
    """The active file is read as a private file, never through a link.

    Following a link would let the reader leave the store directory or grow
    without bound, so the pin reports a closed corrupt fault instead.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    active_records(store, 0, 1)
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_bytes(store.read_bytes())
    outside.chmod(0o600)
    store.unlink()
    store.symlink_to(outside)

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        outcome_history.read_history(SegmentLayout.beside(store))

    assert raised.value.reason == "corrupt", "a link is a closed fault"


def test_an_active_file_past_the_store_bound_is_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active file larger than the store bound is refused, not read."""
    store = tmp_path / "cache-outcomes.jsonl"
    active_records(store, 0, 1)
    monkeypatch.setattr(outcome_records, "MAX_STORE_BYTES", 8)

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        outcome_history.read_history(SegmentLayout.beside(store))

    assert raised.value.reason == "corrupt", "the bound is closed"
