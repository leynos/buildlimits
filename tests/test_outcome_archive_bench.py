"""The archive benchmark's measurements and its bounded memory claim.

The benchmark is the executable form of the requirement that archiving a
15 GB history fits the supervisor's memory budget. These tests keep the
measurements honest: the report must state what it measured, and the streaming
reader must hold one chunk rather than one segment.
"""

import json
import tempfile
import tracemalloc
import typing as typ
from pathlib import Path

import pytest

from buildlimits import outcome_archive_bench
from buildlimits.outcome_archive_bench import CHUNK_BYTES, measure, peak_rows
from buildlimits.outcome_record_format import DecodedStore
from buildlimits.outcome_record_stream import LineStream
from buildlimits.outcome_store_io import read_store

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

_ROW_BYTES = 800
_STREAM_CHUNK = 4096


def _segment(directory: Path, records: int) -> Path:
    """Write a plain segment with one header and ``records`` padded rows.

    Returns
    -------
    Path
        The segment file, readable only by its owner.
    """
    directory.mkdir(parents=True, exist_ok=True)
    lines = ['{"format":"buildlimits-cache-outcomes","version":1}']
    lines.extend(
        json.dumps({"kind": "outcome", "pad": "x" * _ROW_BYTES}) for _ in range(records)
    )
    path = directory / f"segment-{records}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _peak_in_chunks(path: Path, chunk_bytes: int) -> int:
    """Return the most rows a chunk split of ``path`` ever holds at once.

    Returns
    -------
    int
        Peak complete rows within one chunk.
    """
    data = path.read_bytes()
    return max(
        peak_rows(data[offset : offset + chunk_bytes])
        for offset in range(0, len(data), chunk_bytes)
    )


@pytest.mark.parametrize("records", [800, 8000])
def test_peak_buffering_follows_the_chunk_not_the_segment(
    tmp_path: Path, records: int
) -> None:
    """A tenfold longer segment buffers the same number of rows."""
    path = _segment(tmp_path, records)

    report = measure(path, preset=1)

    assert report.peak_rows == _peak_in_chunks(path, CHUNK_BYTES), (
        "the report states the peak the chunk split actually produces"
    )
    assert report.peak_rows <= CHUNK_BYTES // _ROW_BYTES + 1, (
        "one chunk cannot hold more rows than fit in it, however long the segment is"
    )


def test_a_generated_report_names_every_measurement(tmp_path: Path) -> None:
    """The report states the sizes, count, ratio, chunk and streaming peak."""
    source = _segment(tmp_path, 20)

    report = measure(source, preset=1)

    assert report.source_bytes == source.stat().st_size, "source size is measured"
    assert report.record_count == 20, "the header is not a record"
    assert report.archive_bytes > 0, "the archive is written"
    assert 0 < report.ratio <= 1.5, "the ratio compares archive with source"
    assert report.chunk_bytes == CHUNK_BYTES, "the streaming chunk is the codec's"
    assert report.peak_rows >= 1, "a report always names a peak"


def test_main_measures_a_supplied_segment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A path on the command line is measured instead of a generated segment."""
    source = _segment(tmp_path, 12)

    status = outcome_archive_bench.main([str(source)])
    report = json.loads(capsys.readouterr().out)

    assert status == 0, "a supplied segment is measured"
    assert report["record_count"] == 12, "the supplied segment is the one measured"


def test_the_generated_segment_counts_only_its_records(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The generated input is shaped like a sealed segment, header included."""
    status = outcome_archive_bench.main([])
    report = json.loads(capsys.readouterr().out)

    assert status == 0, "a segment is generated and measured"
    assert report["record_count"] == 5_000, "the generated header is not a record"


def test_report_round_trips_as_closed_json(
    tmp_path: Path, snapshot: SnapshotAssertion
) -> None:
    """A report renders as stable JSON a reviewer can diff between runs."""
    source = _segment(tmp_path, 5)

    rendered = measure(source, preset=1).as_dict()

    assert json.loads(json.dumps(rendered, sort_keys=True)) == rendered, (
        "the report is plain JSON"
    )
    assert sorted(rendered) == snapshot, "the schema is closed"


def test_measure_states_the_time_and_scratch_it_used(tmp_path: Path) -> None:
    """The report carries the figures a timer unit's limits are sized from."""
    source = _segment(tmp_path, 50)

    report = measure(source, preset=1)

    assert report.elapsed_seconds >= 0.0, "the pass is timed"
    assert report.temp_bytes == report.archive_bytes, (
        "the scratch disk is the temporary archive's own size"
    )
    assert report.peak_rss_bytes > 0, "the resident peak is read from the kernel"
    assert report.peak_python_bytes > 0, "the Python peak is measured"


def test_measure_leaves_a_callers_tracing_running(tmp_path: Path) -> None:
    """The benchmark stops only the tracer it started itself."""
    source = _segment(tmp_path, 5)
    tracemalloc.start()
    try:
        measure(source, preset=1)
        assert tracemalloc.is_tracing(), "the caller's tracing is not stopped"
    finally:
        tracemalloc.stop()


def test_measure_preserves_a_callers_traced_peak(tmp_path: Path) -> None:
    """A caller's earlier allocation peak survives the benchmark.

    ``reset_peak`` would discard it, so the caller would observe only the
    smaller peak this pass reached and could not tell which pass allocated
    what. The report leaves its traced figure at zero rather than claiming the
    caller's peak as its own. The allocation that establishes the caller's
    peak is released before the pass runs, so a reset could not hide behind
    memory that is still held while it is measured.
    """
    source = _segment(tmp_path, 5)
    # Comfortably above the pass's own peak, which is dominated by the
    # compressor's dictionary, so the caller's figure can only survive if the
    # benchmark left it alone.
    earlier = 16 * 1_048_576

    tracemalloc.start()
    try:
        measure(source, preset=1)
        pass_peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    tracemalloc.start()
    try:
        held = bytearray(earlier)
        caller_peak = tracemalloc.get_traced_memory()[1]
        del held
        report = measure(source, preset=1)
        after = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert caller_peak >= earlier, "the caller's own peak is established"
    assert pass_peak < caller_peak, "the pass alone stays below the caller's peak"
    assert after >= caller_peak, "the caller's peak is not reset by the pass"
    assert report.peak_python_bytes == 0, (
        "the caller's traced peak is not reported as this pass's allocation"
    )


def test_the_generated_segment_is_decodable() -> None:
    """The generated input is a segment the production decoder accepts.

    A shape the decoder rejects would benchmark a document the archiver never
    meets, so the report's record count would describe a store that cannot
    exist.
    """
    with tempfile.TemporaryDirectory() as scratch:
        path = outcome_archive_bench.generate_segment(
            Path(scratch) / "segment-generated.jsonl"
        )
        decoded = read_store(path)

    assert isinstance(decoded, DecodedStore), "the generated segment decodes"
    assert len(decoded.records) == outcome_archive_bench.GENERATED_RECORDS, (
        "every generated row is a record, and the header is not"
    )


def test_measure_leaves_an_untraced_process_untraced(tmp_path: Path) -> None:
    """A plain measurement closes the tracer instead of leaking it."""
    source = _segment(tmp_path, 5)
    was_tracing = tracemalloc.is_tracing()

    measure(source, preset=1)

    assert tracemalloc.is_tracing() == was_tracing, (
        "the tracer's state is exactly what the caller left"
    )


def test_an_incomplete_tail_is_not_a_buffered_row() -> None:
    """A row still streaming across a chunk boundary holds no buffered row."""
    assert peak_rows(b"one\ntwo\nthr") == 2, "only complete rows are counted"


def test_streaming_works_through_a_bounded_source() -> None:
    """The reader consumes one bounded chunk at a time through its protocol."""
    data = (b"row\n" * 10_000) + b"tail"
    stream = LineStream(_Chunked(data, _STREAM_CHUNK), chunk_bytes=_STREAM_CHUNK)

    assert len(list(stream)) == 10_001, "every complete and final row is read"
    assert stream.bytes_read == len(data), "the stream hashed every byte"


def test_traced_memory_stays_far_below_the_segment_size(tmp_path: Path) -> None:
    """Streaming a multi-megabyte segment does not allocate it in memory."""
    source = _segment(tmp_path, 8_000)

    traced_peak = _traced_peak(source)

    assert source.stat().st_size > 6_000_000, "the segment is large enough to matter"
    assert traced_peak < 1_048_576, (
        "peak traced memory stays under one mebibyte while the segment is over "
        "six megabytes"
    )


def _traced_peak(path: Path) -> int:
    """Return the traced allocation peak while streaming ``path``.

    Returns
    -------
    int
        Peak bytes allocated during the read.
    """
    tracemalloc.start()
    try:
        with path.open("rb") as source:
            for _row in LineStream(source, chunk_bytes=_STREAM_CHUNK):
                pass
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


class _Chunked:
    """A binary source that refuses unbounded reads."""

    def __init__(self, data: bytes, limit: int) -> None:
        """Hold ``data`` and the largest read it will serve."""
        self._data = data
        self._limit = limit
        self._position = 0

    def read(self, size: int = -1, /) -> bytes:
        """Return at most ``limit`` bytes, refusing a whole-file read."""
        assert 0 < size <= self._limit, "a streaming read is always bounded"
        end = min(self._position + size, len(self._data))
        chunk = self._data[self._position : end]
        self._position = end
        return chunk


def test_the_benchmark_names_its_own_chunk() -> None:
    """The benchmark reports the codec's chunk, not a private guess."""
    assert outcome_archive_bench.CHUNK_BYTES == 64 * 1024, "the chunk is 64 KiB"
