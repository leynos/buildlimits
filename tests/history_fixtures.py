"""Segmented stores the history readers are tested against.

A history test needs a real store: rows the writer accepts, segments it seals
and an archiver that publishes them. These helpers build that store so a test
only states the shape it is about, and the reading and command suites share one
definition of what a generated history looks like.
"""

import datetime as dt
import typing as typ

from archive_seal_helpers import seal_active
from outcome_archive_helpers import write as write_store
from outcome_test_helpers import attributed_record
from segment_test_helpers import history_policy

from buildlimits import outcome_segment_inventory
from buildlimits.outcome_archive import RunOptions, run_archive
from buildlimits.outcome_record_format import (
    DecodedStore,
    OutcomeRecord,
    StoreRecord,
    WindowSummary,
)
from buildlimits.outcome_segments import SegmentLayout
from buildlimits.outcome_store_io import read_store

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from buildlimits.outcome_segment_inventory import Inventory

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
SALT_NAME = "cache-outcomes.salt"


def records(first: int, count: int) -> tuple[OutcomeRecord, ...]:
    """Build ``count`` attributed records observed one minute apart.

    Parameters
    ----------
    first : int
        The minute offset of the oldest record from `NOW`.
    count : int
        How many records to build.

    Returns
    -------
    tuple[OutcomeRecord, ...]
        The records, oldest observation first.
    """
    return tuple(
        attributed_record(observed_at=NOW - dt.timedelta(minutes=index))
        for index in range(first, first + count)
    )


def window(first: int, count: int) -> WindowSummary:
    """Build one window summary carrying ``count`` requests.

    Parameters
    ----------
    first : int
        The minute offset of the window from `NOW`.
    count : int
        The number of non-compile requests the window reports.

    Returns
    -------
    WindowSummary
        The summary, so a test can recognize it among outcome records.
    """
    return WindowSummary(
        observed_at=NOW - dt.timedelta(minutes=first),
        server_version="1.0.0",
        window_groups=1,
        requests_not_compile=count,
        requests_not_cacheable=0,
        predicted_not_compile=0,
        predicted_declined=0,
        predicted_cacheable=0,
    )


def archived_segments(path: Path, count: int) -> None:
    """Seal ``count`` one-record segments and archive them.

    Parameters
    ----------
    path : Path
        The store path; its layout sits beside it.
    count : int
        How many segments to seal and archive.

    Raises
    ------
    RuntimeError
        If the archiver does not publish one archive per sealed source.
    """
    policy = history_policy(1)
    write_store(path, records(0, count), policy)
    report = run_archive(SegmentLayout.beside(path), policy, RunOptions(now=NOW))
    actions = [action.action for action in report.actions]
    if report.status != "ok" or actions != ["archived"] * count:
        # A fixture that did not produce the store it promises would make the
        # test below fail for the wrong reason, so say so where it happened.
        msg = f"the fixture archived {report.status}: {actions}"
        raise RuntimeError(msg)


def active_records(path: Path, first: int, count: int) -> None:
    """Append records that stay in the active file.

    Parameters
    ----------
    path : Path
        The store path.
    first : int
        The minute offset of the oldest appended record from `NOW`.
    count : int
        How many records to append.
    """
    write_store(path, records(first, count), history_policy(10**6))


def start_domain(path: Path, *, first: int, count: int) -> str:
    """Rotate the host salt, append rows in the new domain and name it.

    Parameters
    ----------
    path : Path
        The store path; its salt sits beside it.
    first : int
        The minute offset of the oldest appended record from `NOW`.
    count : int
        How many records the new domain appends.

    Returns
    -------
    str
        The new domain's salt identifier.
    """
    (path.parent / SALT_NAME).unlink()
    active_records(path, first, count)
    return typ.cast("DecodedStore", read_store(path)).header.salt_id


def seal_once_after_scan(
    monkeypatch: pytest.MonkeyPatch,
    rows: tuple[StoreRecord, ...],
    sequence: int,
) -> None:
    """Seal the active rows once, after the first inventory scan reports.

    The replacement reports the pre-seal inventory and then applies the seal,
    so a reader meets the store exactly as it is when a writer seals during a
    read.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        The test's patcher, which owns undoing the replacement.
    rows : tuple[StoreRecord, ...]
        The rows the seal closes into the new source.
    sequence : int
        The sequence the new source takes.
    """
    real = outcome_segment_inventory.read_inventory
    sealed = False

    def scan_then_seal(
        target: SegmentLayout, *, retention_floor: int | None = None
    ) -> Inventory:
        """Report the pre-seal inventory, then seal the rows once.

        Returns
        -------
        Inventory
            The inventory taken before the seal was applied.
        """
        nonlocal sealed
        inventory = real(target, retention_floor=retention_floor)
        if not sealed:
            sealed = True
            target.sealed_dir.mkdir(parents=True, exist_ok=True)
            seal_active(target, rows, sequence)
        return inventory

    monkeypatch.setattr(outcome_segment_inventory, "read_inventory", scan_then_seal)


def observed(rows: tuple[StoreRecord, ...]) -> tuple[dt.datetime, ...]:
    """Return the observation times of decoded records in order.

    Parameters
    ----------
    rows : tuple[StoreRecord, ...]
        The decoded records.

    Returns
    -------
    tuple[datetime, ...]
        Each record's ``observed_at``.
    """
    return tuple(record.observed_at for record in rows)
