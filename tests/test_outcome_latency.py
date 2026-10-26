"""Opt-in host measurements for recorder-loop and store-queue overhead."""

import collections.abc as cabc
import math
import os
import time
from pathlib import Path

import pytest
from outcome_shell_test_support import make_outcome_world
from outcome_store_helpers import window_summary

from buildlimits.outcome_store import StoreWriter

pytestmark = [
    pytest.mark.sccache_host_check,
    pytest.mark.timeout(60),
    pytest.mark.skipif(
        os.environ.get("BUILD_LIMITS_SCCACHE_HOST_CHECK") != "1",
        reason="set BUILD_LIMITS_SCCACHE_HOST_CHECK=1 to measure host latency",
    ),
]

_SAMPLES = 1_000


def test_recorder_tick_and_store_enqueue_stay_within_latency_budget(
    tmp_path: Path,
) -> None:
    """Measure p99 loop overhead and asynchronous append enqueue cost."""
    world = make_outcome_world(tmp_path)
    writer = StoreWriter(tmp_path / "latency" / "cache-outcomes.jsonl")
    try:
        baseline = _measure(lambda: None)
        world.shell.on_tick(0.0)
        tick_time = 0.0

        def poll_recorder() -> None:
            """Advance a pending query on the same simulated clock as its ticket."""
            nonlocal tick_time
            tick_time += 0.01
            world.shell.on_tick(tick_time)

        recorder = _measure(poll_recorder)
        tick_lengthening_ms = max(0, _p99(recorder) - _p99(baseline)) / 1_000_000
        assert _writer_ready(writer), "the asynchronous writer completed startup"
        enqueue_ms = _measure_enqueue(writer)
        append_p99_ms = _p99(enqueue_ms) / 1_000_000
    finally:
        world.shell.close(0.1)
        writer.close(1.0)

    print(f"outcome tick lengthening p99: {tick_lengthening_ms:.3f} ms")
    print(f"outcome append enqueue p99: {append_p99_ms:.3f} ms")
    assert tick_lengthening_ms <= 5.0, "recorder work stays within the tick budget"
    assert append_p99_ms <= 1.0, "append only enqueues work on the caller"


def _measure(action: cabc.Callable[[], object]) -> list[int]:
    """Collect wall-clock durations for one fixed, repeatable operation."""
    samples = []
    for _ in range(_SAMPLES):
        started = time.perf_counter_ns()
        action()
        samples.append(time.perf_counter_ns() - started)
    return samples


def _measure_enqueue(writer: StoreWriter) -> list[int]:
    """Measure the public append call while the sole worker owns disk I/O."""
    record = window_summary()
    samples = []
    for _ in range(_SAMPLES):
        started = time.perf_counter_ns()
        writer.append(record)
        samples.append(time.perf_counter_ns() - started)
    return samples


def _writer_ready(writer: StoreWriter) -> bool:
    """Wait briefly for startup compaction without consuming the writer result."""
    deadline = time.monotonic() + 5.0
    compacted = False
    while time.monotonic() < deadline:
        compacted |= any(
            result.operation == "compact" and result.reason == "none"
            for result in writer.results()
        )
        if compacted:
            return True
        time.sleep(0.005)
    return False


def _p99(samples: list[int]) -> int:
    """Return the nearest-rank 99th percentile from non-empty samples."""
    if not samples:
        raise ValueError("latency samples must not be empty")
    ordered = sorted(samples)
    return ordered[math.ceil(len(ordered) * 0.99) - 1]
