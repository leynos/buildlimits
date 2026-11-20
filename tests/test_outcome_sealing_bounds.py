"""Sequence choice and row detection around a seal.

The counter is advisory: a lost or damaged one falls back to the names already
on disk, and both the sealed and archived directories count. A seal that
cannot tell whether the active file holds rows must not replace it.
"""

import datetime as dt
from pathlib import Path
from unittest import mock

import pytest
from outcome_store_helpers import close_writer, segment_writer, wait_for_results
from outcome_test_helpers import attributed_record
from segment_test_helpers import history_policy, write_private

from buildlimits import outcome_store_sealing
from buildlimits.outcome_record_format import DecodedStore
from buildlimits.outcome_segments import archive_name, segment_name
from buildlimits.outcome_store_io import read_store

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)


def _directories(root: Path) -> tuple[Path, Path, Path]:
    """Return the counter, sealed and archive paths below ``root``.

    Returns
    -------
    tuple[Path, Path, Path]
        The three paths a seal consults.
    """
    return (
        root / "cache-outcomes.seq",
        root / "cache-outcomes.sealed",
        root / "cache-outcomes.archive",
    )


def test_an_archived_name_is_never_reused(tmp_path: Path) -> None:
    """A store whose sealed sources were all archived still counts them."""
    sequence, sealed, archive = _directories(tmp_path)
    sealed.mkdir()
    archive.mkdir()
    write_private(archive / archive_name(7), b"xz")

    assert outcome_store_sealing.next_sequence(sequence, sealed, archive) == 8, (
        "the archive directory raises the sequence the counter never saw"
    )


def test_the_counter_outranks_both_directories(tmp_path: Path) -> None:
    """A readable counter that is ahead wins over lower on-disk names."""
    sequence, sealed, archive = _directories(tmp_path)
    sealed.mkdir()
    archive.mkdir()
    write_private(archive / archive_name(7), b"xz")
    write_private(sequence, b"10\n")

    assert outcome_store_sealing.next_sequence(sequence, sealed, archive) == 10, (
        "the recorded next sequence is honoured"
    )


def test_a_damaged_counter_falls_back_to_the_names(tmp_path: Path) -> None:
    """An unusable counter never makes the sequence restart at one."""
    sequence, sealed, archive = _directories(tmp_path)
    sealed.mkdir()
    write_private(sealed / segment_name(3), b"rows")
    write_private(sequence, b"not a number")

    assert outcome_store_sealing.next_sequence(sequence, sealed, archive) == 4, (
        "the sealed directory supplies the floor"
    )


def test_an_unmeasurable_active_file_is_not_reported_empty(tmp_path: Path) -> None:
    """The row check raises rather than guess that a file holds nothing."""
    store = write_private(tmp_path / "cache-outcomes.jsonl", b"rows")

    with (
        mock.patch.object(
            outcome_store_sealing, "store_size", side_effect=OSError("unreadable")
        ),
        pytest.raises(OSError, match="unreadable"),
    ):
        outcome_store_sealing.store_has_rows(store)


def test_a_seal_that_cannot_measure_the_active_file_keeps_its_rows(
    tmp_path: Path,
) -> None:
    """A failed measurement cannot replace a populated file with a header."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(), NOW)
    try:
        writer.append(attributed_record(observed_at=NOW))
        wait_for_results(writer, operation="append", reason="none")
        with mock.patch.object(
            outcome_store_sealing, "store_size", side_effect=OSError("unreadable")
        ):
            writer.append(attributed_record(observed_at=NOW + dt.timedelta(minutes=1)))
            results = wait_for_results(writer, operation="seal", reason="save_failed")
            assert results, "the failed seal is reported"
            decoded = read_store(store)
            assert isinstance(decoded, DecodedStore), "the active file still decodes"
            assert [record.observed_at for record in decoded.records] == [
                NOW,
                NOW + dt.timedelta(minutes=1),
            ], "both rows are still in the active file"
    finally:
        close_writer(writer)
