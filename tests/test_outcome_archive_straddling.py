"""Composing an age trim with the record cap in one archive run.

A count trim is a prefix over the document's row positions, while an age or
byte trim removes rows the count never measured. These tests pin that the
count is decided only after the other trim has been applied and measured, so a
row the policy allows is never discarded by the composition.
"""

import datetime as dt
import lzma
import typing as typ
from pathlib import Path

from outcome_archive_helpers import NOW, document, layout, policy, run
from outcome_test_helpers import attributed_record
from segment_test_helpers import write_private

from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segments import archive_name, segment_name

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import StoreRecord


def _straddling_rows() -> tuple[dt.datetime, tuple[StoreRecord, ...]]:
    """Build one segment whose expired rows straddle an eligible row.

    The ordering is *expired, at cutoff, expired, fresh*: the archive's first
    and last rows span the cutoff, so the age policy plans a prefix trim, while
    the eligible row at the cutoff sits between two expired ones. A count trim
    taken over the original positions therefore cannot be composed with the age
    trim without dropping that eligible row.

    Returns
    -------
    tuple[datetime, tuple[StoreRecord, ...]]
        The cutoff and the records in file order.
    """
    cutoff = NOW - dt.timedelta(days=30)
    rows = (
        attributed_record(observed_at=cutoff - dt.timedelta(days=1)),
        attributed_record(observed_at=cutoff),
        attributed_record(observed_at=cutoff - dt.timedelta(hours=1)),
        attributed_record(observed_at=NOW),
    )
    return cutoff, rows


def _write_straddling_store(root: Path) -> tuple[dt.datetime, tuple[StoreRecord, ...]]:
    """Seed one sealed segment holding the straddling rows.

    The source is written directly because the writer drops a row that arrives
    already expired; the archiver applies the run's shorter bound to the
    segment it finds.

    Returns
    -------
    tuple[datetime, tuple[StoreRecord, ...]]
        The cutoff and the records the segment holds.
    """
    cutoff, rows = _straddling_rows()
    sealed = layout(root).sealed_dir
    sealed.mkdir(parents=True)
    write_private(sealed / segment_name(1), document(rows))
    return cutoff, rows


def _archived_document(root: Path, sequence: int) -> bytes:
    """Return the decompressed document one archive holds.

    Returns
    -------
    bytes
        The archive's plain JSON Lines content.
    """
    archive = layout(root).archive_dir / archive_name(sequence)
    return lzma.decompress(archive.read_bytes())


def test_an_age_trim_does_not_cost_an_eligible_row_to_the_record_cap(
    tmp_path: Path,
) -> None:
    """A cap counts the rows an age trim frees before it trims by position.

    The age trim alone leaves the two eligible rows, which is exactly the cap.
    A count trim decided over the original positions would also drop the row at
    the cutoff and keep only the fresh one.
    """
    _cutoff, rows = _write_straddling_store(tmp_path)

    report = run(tmp_path, policy(1_000_000, max_age_days=30, max_records=2))

    assert report.status == "ok", "the composition is not a failure"
    assert _archived_document(tmp_path, 1) == document((rows[1], rows[3])), (
        "the row at the cutoff and the fresh row both survive"
    )
    assert report.retained_records == 2, "the cap is met by the age trim alone"
    assert read_inventory(layout(tmp_path)).archived[0].record_count == 2, (
        "the archive's sidecar records the rows it kept"
    )


def test_a_record_cap_after_an_age_trim_drops_only_the_oldest_eligible_row(
    tmp_path: Path,
) -> None:
    """The follow-up run applies the count to the measured age-trimmed rows.

    With a cap of one, the age trim still frees both expired rows, and the
    count trim then drops the oldest eligible row rather than reaching past it.
    The trim is decided in a later pass of the same run, from the count the
    rewritten sidecar records.
    """
    _cutoff, rows = _write_straddling_store(tmp_path)

    report = run(tmp_path, policy(1_000_000, max_age_days=30, max_records=1))

    assert report.status == "ok", "the deferred count trim is not a failure"
    assert _archived_document(tmp_path, 1) == document((rows[3],)), (
        "only the oldest eligible row leaves"
    )
    assert report.retained_records == 1, "the cap is met in one firing"
    assert [(action.action, action.sequence) for action in report.actions] == [
        ("archived", 1),
        ("trimmed", 1),
        ("trimmed", 1),
    ], "the age trim and the count trim each rewrite the archive"
