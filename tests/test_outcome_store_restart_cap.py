"""A reduced record cap reaches a segmented store at its next start."""

import datetime as dt
from pathlib import Path

from outcome_store_helpers import (
    append_and_wait,
    close_writer,
    decoded_store,
    segment_writer,
)
from outcome_test_helpers import window_summary

from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segments import SegmentLayout, segment_name

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
        sealed = sorted(layout.sealed_dir.glob("*.jsonl"))
        assert [path.name for path in sealed] == [segment_name(1)], (
            "the over-cap active file is sealed at restart"
        )
        assert len(decoded_store(sealed[0]).records) == 3, "every row reaches the seal"
        assert decoded_store(store).records == (), "the active file restarts empty"
    finally:
        close_writer(writer, 1.0)
