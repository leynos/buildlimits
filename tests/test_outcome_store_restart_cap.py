"""A reduced record cap reaches a segmented store at its next start."""

import datetime as dt
import lzma
from pathlib import Path

from outcome_archive_helpers import policy as archive_policy
from outcome_archive_helpers import run as archive_run
from outcome_store_helpers import (
    append_and_wait,
    close_writer,
    decoded_store,
    segment_writer,
    wait_for_results,
)
from outcome_test_helpers import attributed_record, window_summary

from buildlimits import outcome_records
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segments import SegmentLayout, archive_name, segment_name

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
_SEGMENT_BYTES = 10_000_000


def _segmented_policy(*, max_records: int | None) -> RetentionPolicy:
    """Return a segmented policy that keeps the active file below one segment.

    Returns
    -------
    RetentionPolicy
        An unlimited byte budget with the requested record cap.
    """
    return RetentionPolicy(
        max_age=dt.timedelta(days=30),
        max_bytes=0,
        max_records=max_records,
        headroom_bytes=_SEGMENT_BYTES * 4,
        segment_bytes=_SEGMENT_BYTES,
    )


def test_a_reduced_cap_seals_an_idle_segmented_store_at_restart(
    tmp_path: Path,
) -> None:
    """An idle store cannot hold more rows than its reduced cap allows.

    The active file is below the segment size and holds no expired row, so a
    restart would previously leave it untouched. The archiver cannot trim the
    active file, so a store that never appends again would stay over the cap;
    the restart must seal it and hand the rows to the archiver.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    layout = SegmentLayout.beside(store)
    writer = segment_writer(store, _segmented_policy(max_records=None), NOW)
    try:
        for _ in range(3):
            append_and_wait(writer, window_summary(observed_at=NOW))
    finally:
        close_writer(writer, 1.0)
    assert decoded_store(store).records, "the fixture wrote rows to the active file"

    writer = segment_writer(store, _segmented_policy(max_records=2), NOW)
    try:
        # Startup publishes its load result before the settle pass seals the
        # file, so the seal result is the one that says the new active file is
        # in place. Reading the directory after only the load result races the
        # worker between its rename and the fresh header.
        wait_for_results(writer, operation="seal", reason="none")
        sealed = sorted(layout.sealed_dir.glob("*.jsonl"))
        assert [path.name for path in sealed] == [segment_name(1)], (
            "the over-cap active file is sealed at restart"
        )
        assert len(decoded_store(sealed[0]).records) == 3, "every row reaches the seal"
        assert decoded_store(store).records == (), "the active file restarts empty"
    finally:
        close_writer(writer, 1.0)


def test_a_restart_seal_is_trimmed_by_a_settled_archive_run(tmp_path: Path) -> None:
    """A sealed over-cap file reaches the cap once the archiver has run.

    A restart can only seal the rows the writer holds; the archiver applies the
    cap to closed history. Run both in sequence and assert that the archived
    segment keeps the two later rows, so the enforced end state meets the cap.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    layout = SegmentLayout.beside(store)
    rows = (
        attributed_record(observed_at=NOW - dt.timedelta(minutes=1)),
        attributed_record(observed_at=NOW - dt.timedelta(minutes=2)),
        attributed_record(observed_at=NOW - dt.timedelta(minutes=3)),
    )
    writer = segment_writer(store, _segmented_policy(max_records=None), NOW)
    try:
        for row in rows:
            append_and_wait(writer, row)
    finally:
        close_writer(writer, 1.0)

    writer = segment_writer(store, _segmented_policy(max_records=2), NOW)
    try:
        wait_for_results(writer, operation="seal", reason="none")
    finally:
        close_writer(writer, 1.0)

    report = archive_run(tmp_path, archive_policy(1_000_000, max_records=2))

    assert report.status == "ok", "the settled archive run is not a failure"
    archived = outcome_records.decode_lines(
        lzma.decompress((layout.archive_dir / archive_name(1)).read_bytes()).decode(
            "utf-8"
        )
    )
    assert archived.records == rows[1:], (
        "the archive keeps the two later rows and drops the leading one"
    )
    assert report.retained_records == 2, "the settled store meets the reduced cap"
