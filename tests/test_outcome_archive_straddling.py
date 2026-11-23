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
    import collections.abc as cabc

    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_retention import RetentionPolicy


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


def _write_segments(root: Path, segments: tuple[tuple[StoreRecord, ...], ...]) -> None:
    """Seed one sealed source per document, in sequence order.

    The sources are written directly because the writer drops a row that
    arrives already expired; the archiver applies the run's shorter bound to
    the segments it finds.
    """
    sealed = layout(root).sealed_dir
    sealed.mkdir(parents=True)
    for sequence, rows in enumerate(segments, start=1):
        write_private(sealed / segment_name(sequence), document(rows))


def _write_eviction_fixture(
    root: Path, expired_rows: int
) -> tuple[tuple[StoreRecord, ...], StoreRecord]:
    """Seed an eligible segment beside one that straddles the cutoff.

    The first segment holds two eligible rows. The second holds ``expired_rows``
    rows older than the cutoff and one fresh row, so an age trim frees most of
    its rows while both segments keep eligible history.

    Returns
    -------
    tuple[tuple[StoreRecord, ...], StoreRecord]
        The first segment's rows and the second segment's fresh row.
    """
    cutoff = NOW - dt.timedelta(days=30)
    first = (
        attributed_record(observed_at=NOW - dt.timedelta(hours=2)),
        attributed_record(observed_at=NOW - dt.timedelta(hours=1)),
    )
    expired = tuple(
        attributed_record(
            observed_at=cutoff - dt.timedelta(minutes=expired_rows - index)
        )
        for index in range(expired_rows)
    )
    fresh = attributed_record(observed_at=NOW)
    _write_segments(root, (first, (*expired, fresh)))
    return first, fresh


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


def test_a_record_cap_waits_for_age_eviction_across_segments(tmp_path: Path) -> None:
    """A cap deletes nothing whole while an age trim still owns older rows.

    Sequence 1 holds two eligible rows and sequence 2 holds two expired rows
    and one eligible row, so age eviction alone leaves three rows, exactly the
    cap. A walk that counted the expired rows as retained would delete sequence
    1 whole and keep only the fresh row of sequence 2.
    """
    first, fresh = _write_eviction_fixture(tmp_path, 2)

    report = run(tmp_path, policy(1_000_000, max_age_days=30, max_records=3))

    assert report.status == "ok", "the deferred cap is not a failure"
    assert _archived_document(tmp_path, 1) == document(first), (
        "the older eligible segment survives whole"
    )
    assert _archived_document(tmp_path, 2) == document((fresh,)), (
        "the newer segment keeps only its eligible row"
    )
    assert [(action.action, action.sequence) for action in report.actions] == [
        ("archived", 1),
        ("archived", 2),
        ("trimmed", 2),
    ], "no whole deletion is chosen before the age trim is measured"
    assert report.retained_records == 3, "the cap is met by age eviction alone"


def test_a_byte_budget_waits_for_age_eviction_across_segments(tmp_path: Path) -> None:
    """A byte budget deletes nothing whole while an age trim is unmeasured.

    Sequence 2 holds forty expired rows and one fresh row, so its age trim
    frees far more bytes than sequence 1 occupies. A byte walk that counted
    the expired bytes as retained would delete sequence 1 whole, even though
    the settled store fits the budget exactly.
    """
    first, fresh = _write_eviction_fixture(tmp_path, 40)
    run(tmp_path, policy(1_000_000, max_age_days=3650))
    published = {
        entry.sequence: entry.logical_bytes
        for entry in read_inventory(layout(tmp_path)).archived
    }
    budget = published[1] + len(document((fresh,)))

    report = run(tmp_path, policy(1_000_000, max_age_days=30, max_bytes=budget))

    assert report.status == "ok", "the deferred byte budget is not a failure"
    assert _archived_document(tmp_path, 1) == document(first), (
        "the older eligible segment survives whole"
    )
    assert _archived_document(tmp_path, 2) == document((fresh,)), (
        "the newer segment keeps only its eligible row"
    )
    assert not [action for action in report.actions if action.action == "deleted"], (
        "no whole deletion is chosen before the age trim is measured"
    )
    assert report.retained_bytes == budget, "the settled store fits the budget"


def _assert_a_capacity_trim_follows_append_order(
    tmp_path: Path,
    make_policy: cabc.Callable[[StoreRecord], RetentionPolicy],
    *,
    expect_budget: bool = False,
) -> None:
    """Assert one capacity bound trims the leading closed-history row.

    Parameters
    ----------
    tmp_path : Path
        The store root the archiver reads.
    make_policy : Callable[[StoreRecord], RetentionPolicy]
        Builds the policy from the older observation, so a byte budget can
        size itself against the row that must survive.
    expect_budget : bool
        Whether the byte budget is expected to be met exactly.
    """
    newer_observation = attributed_record(observed_at=NOW - dt.timedelta(hours=1))
    older_observation = attributed_record(observed_at=NOW - dt.timedelta(hours=2))
    sealed = layout(tmp_path).sealed_dir
    sealed.mkdir(parents=True)
    write_private(
        sealed / segment_name(1), document((newer_observation, older_observation))
    )

    retention = make_policy(older_observation)
    report = run(tmp_path, retention)

    assert report.status == "ok", "the trim is not a failure"
    assert _archived_document(tmp_path, 1) == document((older_observation,)), (
        "the later-appended row survives the capacity trim"
    )
    if expect_budget:
        assert report.retained_bytes == retention.max_bytes, (
            "the archive meets the budget exactly"
        )


def test_a_record_cap_trims_closed_history_in_append_order(tmp_path: Path) -> None:
    """Closed history follows the same append-order rule as the writer.

    A producer can write a row observed earlier after one observed later, so
    the two orders can disagree. The record cap removes the leading row in
    file order, keeping the later-appended row even though its observation is
    older. The archive is compared exactly, so a change to observation-time
    ordering would fail here rather than stay implicit.
    """
    _assert_a_capacity_trim_follows_append_order(
        tmp_path, lambda _older: policy(1_000_000, max_records=1)
    )


def test_a_byte_budget_trims_closed_history_in_append_order(tmp_path: Path) -> None:
    """A byte trim removes the leading row, whichever observation it holds.

    The budget fits only the later-appended row's document, so the trim must
    drop the leading row even though it carries the newer observation. The
    archive is compared exactly, so observation-time ordering would fail here
    rather than stay implicit.
    """
    _assert_a_capacity_trim_follows_append_order(
        tmp_path,
        lambda older: policy(1_000_000, max_bytes=len(document((older,)))),
        expect_budget=True,
    )
