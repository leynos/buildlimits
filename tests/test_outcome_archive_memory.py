"""Bounded memory across history sizes, segment counts and a large run.

The archiver and the streaming replay must cost memory proportional to one
segment rather than to the retained history, while a materializing read holds
every row it returns. The first test traces each consumer separately at two
sizes: the fixed costs must not grow, and the materializing read must, which is
what proves the traces observe rows at all. The second seals the same history
into far more segments, so the plan's per-segment bookkeeping is measured
rather than assumed. The third measures the peak resident set size of the real
processes over a several-hundred-megabyte history; it is opt-in because
generating that history takes minutes.
"""

import contextlib
import dataclasses as dc
import datetime as dt
import io
import os
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pytest
from archive_cli_helpers import REPO_ROOT, SCRIPT, command_arguments, write_config
from outcome_archive_helpers import NOW, records
from outcome_archive_helpers import write as write_store
from outcome_store_helpers import close_writer, segment_writer
from segment_test_helpers import history_policy

from buildlimits import outcome_history, outcome_replay_cli
from buildlimits.outcome_archive import RunOptions, run_archive
from buildlimits.outcome_segments import SegmentLayout

# Preset 6 dominates every traced peak with liblzma's encoder dictionary, so
# this bound is that dictionary plus this package's own bookkeeping. Both
# measured peaks sit near 94 MiB whatever the row count.
_ARCHIVE_BOUND = 128 * 1024 * 1024
# Four times the rows may not cost measurably more. Measured growth is about
# 0.1 MiB, while buffering the extra 12,000 rows would exceed this allowance
# many times over.
_ARCHIVE_GROWTH = 4 * 1024 * 1024
# A replay pass holds one decoder and one running tally. Measured just above
# nine MiB at both sizes.
_REPLAY_BOUND = 16 * 1024 * 1024
_REPLAY_GROWTH = 2 * 1024 * 1024
# The materializing read must grow with the history; this witnesses that the
# traces would notice a consumer that buffers. Measured growth is 13 MiB.
_READ_GROWTH = 8 * 1024 * 1024

_SMALL_ROWS = 4_000
_LARGE_ROWS = 16_000
_SEGMENT_BYTES = 512 * 1024

# The opt-in test generates several hundred megabytes of history, which takes
# minutes, so it runs only when explicitly requested.
_LARGE_ENV = "BUILDLIMITS_ARCHIVE_LARGE_MEMORY"
_LARGE_HISTORY_BYTES = 320 * 1024 * 1024
_LARGE_SEGMENT_BYTES = 4 * 1024 * 1024
_LARGE_BATCH_ROWS = 20_000
# Far enough in the past that the generated history is ordered, and old enough
# that a real age window would evict it; the configuration disables age
# eviction, so the archiver keeps every row.
_LARGE_START = dt.timedelta(days=400)
# Bounds are peak resident set size in KiB. Over a 320 MB history both the
# archiver and the streaming replay measured about 47,000 KiB, so this bound
# allows three times the observed peak while staying far below the several
# hundred megabytes a buffered history would need.
_RSS_KIB = 150_000


@dc.dataclass(frozen=True, slots=True)
class _Peaks:
    """Traced peaks for one history size, in bytes, and its segment count."""

    archive: int
    read: int
    replay: int
    segments: int


def _measure(root: Path, count: int, *, segment_bytes: int = _SEGMENT_BYTES) -> _Peaks:
    """Generate one history and trace the archiver and each reader in turn.

    Parameters
    ----------
    root : Path
        The directory the generated store lives under.
    count : int
        How many records the history holds.
    segment_bytes : int
        The size at which the writer seals a source, so a smaller value seals
        the same rows into more segments.

    Returns
    -------
    _Peaks
        The traced peak of the archive run, the materializing read and the
        streaming replay, each measured in its own trace, and the number of
        sources the archive run compressed.
    """
    root.mkdir(parents=True, exist_ok=True)
    store = root / "cache-outcomes.jsonl"
    policy = history_policy(segment_bytes)
    write_store(store, records(NOW - dt.timedelta(minutes=count), count), policy)
    layout = SegmentLayout.beside(store)

    tracemalloc.start()
    try:
        report = run_archive(layout, policy, RunOptions(now=NOW))
        _current, archive = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report.status == "ok", "the generated history archives cleanly"
    segments = sum(1 for action in report.actions if action.action == "archived")

    tracemalloc.start()
    try:
        history = outcome_history.read_history_beside(store)
        _current, read = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(history.records) == count, "the materializing read returns every row"

    tracemalloc.start()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            status = outcome_replay_cli.main([
                "h3",
                "--store",
                str(store),
                "--history",
                "--json",
            ])
        _current, replay = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert status == 0, "the streaming replay reads the archived history"
    return _Peaks(archive=archive, read=read, replay=replay, segments=segments)


# Tracing slows the compressor several-fold, and the two sizes are measured in
# one test, so the default per-test timeout is too tight for a loaded host.
@pytest.mark.timeout(300)
def test_memory_is_bounded_at_two_history_sizes(tmp_path: Path) -> None:
    """Four times the history costs the same fixed encoder and decoder memory."""
    small = _measure(tmp_path / "small", _SMALL_ROWS)
    large = _measure(tmp_path / "large", _LARGE_ROWS)

    assert small.archive < _ARCHIVE_BOUND, (
        f"{_SMALL_ROWS} rows peaked at {small.archive} traced bytes"
    )
    assert large.archive < _ARCHIVE_BOUND, (
        f"{_LARGE_ROWS} rows peaked at {large.archive} traced bytes"
    )
    assert large.archive - small.archive < _ARCHIVE_GROWTH, (
        "archiving four times the rows added "
        f"{large.archive - small.archive} traced bytes"
    )
    assert small.replay < _REPLAY_BOUND, (
        f"replaying {_SMALL_ROWS} rows peaked at {small.replay} traced bytes"
    )
    assert large.replay < _REPLAY_BOUND, (
        f"replaying {_LARGE_ROWS} rows peaked at {large.replay} traced bytes"
    )
    assert large.replay - small.replay < _REPLAY_GROWTH, (
        "replaying four times the rows added "
        f"{large.replay - small.replay} traced bytes"
    )
    assert large.read > small.read + _READ_GROWTH, (
        "the materializing read grows with the history, so the traces do observe rows"
    )


@pytest.mark.timeout(300)
def test_memory_does_not_follow_the_segment_count(tmp_path: Path) -> None:
    """Over three times the segments for the same history cost little more.

    Every archive is streamed, but the inventory and the plan hold one small
    record per segment, so the peak would follow the segment count if that
    bookkeeping dominated it. Sealing the same history into quarter-sized
    segments measures the cost instead of assuming it is negligible.
    """
    wide = _measure(tmp_path / "wide", _LARGE_ROWS)
    narrow = _measure(
        tmp_path / "narrow", _LARGE_ROWS, segment_bytes=_SEGMENT_BYTES // 4
    )

    assert narrow.segments > 3 * wide.segments, (
        f"the fixture needs more segments: {narrow.segments} against {wide.segments}"
    )
    assert narrow.archive < _ARCHIVE_BOUND, (
        f"{narrow.segments} segments peaked at {narrow.archive} traced bytes"
    )
    assert narrow.archive - wide.archive < _ARCHIVE_GROWTH, (
        "archiving three times the segments added "
        f"{narrow.archive - wide.archive} traced bytes"
    )


def _sealed_bytes(layout: SegmentLayout) -> int:
    """Return the total size of the plain segments sealed beside the store.

    Returns
    -------
    int
        The bytes currently waiting to be archived.
    """
    if not layout.sealed_dir.exists():
        return 0
    return sum(path.stat().st_size for path in layout.sealed_dir.glob("*.jsonl"))


def _generate_large_store(root: Path, target_bytes: int) -> tuple[Path, int]:
    """Append generated history until the sealed sources reach a size.

    Parameters
    ----------
    root : Path
        The directory the generated store lives under.
    target_bytes : int
        The sealed bytes the history should reach.

    Returns
    -------
    tuple[Path, int]
        The store path and the sealed bytes it now holds.
    """
    root.mkdir(parents=True, exist_ok=True)
    store = root / "cache-outcomes.jsonl"
    layout = SegmentLayout.beside(store)
    writer = segment_writer(store, history_policy(_LARGE_SEGMENT_BYTES), NOW)
    appended = 0
    try:
        while _sealed_bytes(layout) < target_bytes:
            start = NOW - _LARGE_START + dt.timedelta(minutes=appended)
            for row in records(start, _LARGE_BATCH_ROWS):
                writer.append(row)
            appended += _LARGE_BATCH_ROWS
    finally:
        close_writer(writer)
    return store, _sealed_bytes(layout)


def _measure_command(arguments: list[str], *, timeout: int = 1_800) -> tuple[int, int]:
    """Run one command in a fresh process and report status and peak RSS.

    ``RUSAGE_CHILDREN`` aggregates the wrapper's waited-for children and the
    wrapper has exactly one, so the figure is that command's peak resident set
    size in KiB.

    Parameters
    ----------
    arguments : list[str]
        The command and its arguments.
    timeout : int
        How long the command may run before it is killed.

    Returns
    -------
    tuple[int, int]
        The command's exit status and its peak resident set size in KiB.
    """
    wrapper = "\n".join([
        "import resource, subprocess, sys",
        "finished = subprocess.run(sys.argv[1:], check=False, capture_output=True, text=True)",
        "print(finished.stderr, file=sys.stderr, end='')",
        "peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss",
        "print(finished.returncode, peak)",
    ])
    finished = subprocess.run(
        [sys.executable, "-c", wrapper, *arguments],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    status, peak = finished.stdout.split()
    return int(status), int(peak)


@pytest.mark.skipif(
    os.environ.get(_LARGE_ENV) != "1",
    reason=f"set {_LARGE_ENV}=1 to run the several-hundred-megabyte test",
)
@pytest.mark.timeout(1_800)
def test_a_large_history_archives_and_replays_in_bounded_memory(
    tmp_path: Path,
) -> None:
    """A several-hundred-megabyte history costs a fixed peak resident size."""
    store, sealed = _generate_large_store(tmp_path, _LARGE_HISTORY_BYTES)
    assert sealed >= _LARGE_HISTORY_BYTES, "the generated history is large enough"
    config = write_config(tmp_path / "supervisor.toml")
    layout = SegmentLayout.beside(store)

    archived, archive_rss = _measure_command([
        str(SCRIPT),
        "run",
        *command_arguments(store, config),
    ])
    replayed, replay_rss = _measure_command([
        sys.executable,
        "-m",
        "buildlimits.outcome_replay_cli",
        "h3",
        "--store",
        str(store),
        "--history",
        "--json",
    ])

    assert archived == 0, "the archiver completes"
    assert not list(layout.sealed_dir.glob("*.jsonl")), (
        "the archiver consumes the whole history"
    )
    assert replayed == 0, "the streaming replay reads the archived history"
    assert archive_rss < _RSS_KIB, f"archiving peaked at {archive_rss} KiB"
    assert replay_rss < _RSS_KIB, f"replaying peaked at {replay_rss} KiB"
