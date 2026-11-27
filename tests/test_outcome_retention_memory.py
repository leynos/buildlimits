"""Bounded memory for a retention pass that rewrites a sealed source.

A retention-only run streams one row at a time and holds no encoder, so its
working set must not follow the source size. This traces the in-place rewrite
at two sizes and pins that four times the rows cost the same fixed memory.
"""

import datetime as dt
import json
import tracemalloc
from pathlib import Path

import pytest
from outcome_archive_helpers import NOW, document, policy, records
from outcome_test_helpers import attributed_record
from segment_test_helpers import write_private

from buildlimits.outcome_archive import RunOptions, run_retain
from buildlimits.outcome_segments import SegmentLayout, sealed_meta_name, segment_name
from buildlimits.outcome_store_sealing import sealed_metadata

# An in-place trim streams one row at a time and holds no encoder dictionary,
# so its peak is this package's bookkeeping plus one row. Measured well below
# this bound at both sizes.
_RETAIN_BOUND = 24 * 1024 * 1024
_RETAIN_GROWTH = 4 * 1024 * 1024


def _write_straddling_source(root: Path, count: int) -> Path:
    """Write one measured sealed source whose first half is past the cutoff.

    Returns
    -------
    Path
        The store path beside the source.
    """
    root.mkdir(parents=True, exist_ok=True)
    store = root / "cache-outcomes.jsonl"
    cutoff = NOW - dt.timedelta(days=30)
    expired = tuple(
        attributed_record(observed_at=cutoff - dt.timedelta(minutes=count - index))
        for index in range(count)
    )
    rows = (*expired, *records(NOW, count))
    sealed = SegmentLayout.beside(store).sealed_dir
    sealed.mkdir(parents=True, exist_ok=True)
    sealed.chmod(0o700)
    write_private(sealed / segment_name(1), document(rows))
    metadata = sealed_metadata(list(rows), 1, "salt-1", NOW)
    (sealed / sealed_meta_name(1)).write_text(json.dumps(metadata), encoding="utf-8")
    return store


def _measure_retain(root: Path, count: int) -> int:
    """Trace one in-place sealed trim over a straddling source.

    Returns
    -------
    int
        The run's traced peak in bytes.
    """
    store = _write_straddling_source(root, count)
    retention = policy(1_000_000, max_age_days=30, max_bytes=0)

    tracemalloc.start()
    try:
        report = run_retain(SegmentLayout.beside(store), retention, RunOptions(now=NOW))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report.status == "ok", "the in-place trim completes"
    return peak


@pytest.mark.timeout(300)
def test_retention_rewrites_a_sealed_source_in_bounded_memory(
    tmp_path: Path,
) -> None:
    """Four times the rows cost the same one-row streaming rewrite."""
    small = _measure_retain(tmp_path / "small", 2_000)
    large = _measure_retain(tmp_path / "large", 8_000)

    assert small < _RETAIN_BOUND, f"2,000 rows peaked at {small} traced bytes"
    assert large < _RETAIN_BOUND, f"8,000 rows peaked at {large} traced bytes"
    assert large - small < _RETAIN_GROWTH, (
        f"four times the rows added {large - small} traced bytes"
    )
