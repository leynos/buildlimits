"""Capacity enforcement on a firing whose age bound is withheld.

A skewed reading suppresses age eviction but keeps the byte and record bounds.
The follow-up passes must count the active rows the age bound cannot expire, or
the capacity plan understates the store and leaves it over its bound. These
tests reach that path through a sealed source whose metadata must be repaired
first, so the capacity walk can only run in a follow-up pass.
"""

import datetime as dt
import typing as typ

from outcome_archive_helpers import NOW, document, layout, policy
from outcome_test_helpers import records_at
from segment_test_helpers import write_private
from test_outcome_archive_clock_guard import (
    BOOTTIME,
    DAY,
    FRESH,
    no_age,
    reading,
    retain,
)

from buildlimits.outcome_segments import segment_name

CUTOFF = NOW - dt.timedelta(days=30)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreRecord


def _guarded_store(
    tmp_path: Path, kept: tuple[StoreRecord, ...]
) -> tuple[tuple[StoreRecord, ...], Path]:
    """Record a baseline, then add expired active rows and one sealed source.

    The sealed source has no metadata, so the first follow-up pass has to
    repair it before a capacity bound can be planned.

    Returns
    -------
    tuple[tuple[object, ...], Path]
        The active rows and the sealed source path.
    """
    expired = records_at(CUTOFF - dt.timedelta(days=2), 2)
    retain(tmp_path, no_age(), reading=reading(dt.timedelta(0), BOOTTIME))
    write_private(layout(tmp_path).active, document(expired))
    sealed = write_private(
        layout(tmp_path).sealed_dir / segment_name(1), document(kept)
    )
    return expired, sealed


def test_a_skewed_firing_keeps_a_byte_bound_after_a_repair(
    tmp_path: Path,
) -> None:
    """The follow-up scan counts the active rows the age bound cannot expire."""
    expired, sealed = _guarded_store(tmp_path, (FRESH,))

    report = retain(
        tmp_path,
        policy(
            1_000_000,
            max_age_days=30,
            max_bytes=len(document(expired)) + 1,
        ),
        reading=reading(dt.timedelta(days=2), BOOTTIME + 0.5 * DAY),
    )

    assert report.clock_skew is True, "the capacity bound runs on a skewed firing"
    assert report.status == "ok", "the byte bound is met in the same firing"
    assert not sealed.exists(), (
        "the sealed source is evicted against the active rows the guard keeps"
    )
    assert layout(tmp_path).active.read_bytes() == document(expired), (
        "the active file the writer owns is never rewritten"
    )


def test_a_skewed_firing_keeps_a_record_cap_after_a_repair(
    tmp_path: Path,
) -> None:
    """The record cap counts the active rows the age bound cannot expire."""
    keep = records_at(NOW - dt.timedelta(hours=1), 2)
    _expired, sealed = _guarded_store(tmp_path, keep)

    report = retain(
        tmp_path,
        policy(1_000_000, max_age_days=30, max_records=3),
        reading=reading(dt.timedelta(days=2), BOOTTIME + 0.5 * DAY),
    )

    assert report.clock_skew is True, "the cap runs on a skewed firing"
    assert report.status == "ok", "the cap is met in the same firing"
    assert sealed.read_bytes() == document(keep[1:]), (
        "the sealed source keeps only the row that fits the cap"
    )
