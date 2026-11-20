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
from history_fixtures import archived_segments
from segment_test_helpers import write_private

from buildlimits import outcome_history, outcome_segment_inventory
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
    assert calls == 2 * outcome_history._HISTORY_ATTEMPTS, (
        "every attempt both scanned and failed to settle"
    )


def test_a_segment_with_no_header_is_corrupt(tmp_path: Path) -> None:
    """An empty closed segment is a fault, not an empty document."""
    layout = SegmentLayout.beside(tmp_path / "cache-outcomes.jsonl")
    layout.sealed_dir.mkdir(parents=True)
    write_private(layout.sealed_dir / segment_name(1), b"")

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        outcome_history.read_history(layout)

    assert raised.value.reason == "corrupt", "the fault is closed"
