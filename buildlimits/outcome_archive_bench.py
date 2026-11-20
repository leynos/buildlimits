"""Measure archiving one segment without reading it into memory.

The archiver's promise is that a 15 GB retained history is handled in bounded
memory. This benchmark states the size of each input it reads, the physical and
logical size of the result, the largest number of rows the streaming reader
ever buffers, the peak traced allocation during the pass, the elapsed time,
the process's peak resident memory and the scratch disk the pass used. A
regression in memory behaviour is therefore visible in the report rather than
inferred from a stopwatch, and the figures an operator needs to size a timer
unit's ``MemoryMax`` and ``TimeoutStartSec`` come from the same run.

The traced peak covers allocations made through Python's allocator, which
includes liblzma's encoder dictionary; it therefore tracks the XZ preset, not
the segment's length, and ``peak_rss_bytes`` is the figure to compare with a
unit's ``MemoryMax``.

Run ``make outcome-archive-bench`` to print one report for a generated
segment; a caller with a real segment passes its path instead.
"""

import dataclasses as dc
import datetime as dt
import json
import lzma
import os
import resource
import sys
import tempfile
import time
import tracemalloc
import typing as typ
from pathlib import Path

from buildlimits.outcome_key import CacheKey, ToolchainIdentity
from buildlimits.outcome_record_encode import encode_line
from buildlimits.outcome_record_format import OutcomeRecord, StoreHeader
from buildlimits.outcome_record_stream import CHUNK_BYTES, LineStream

if typ.TYPE_CHECKING:
    import collections.abc as cabc

BENCH_SCHEMA = 1
_PRESET = 6
# A generated segment holds enough rows for the chunk split to be visible.
GENERATED_RECORDS = 5_000
_GENERATED_AT = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
# Every key uses this toolchain and observation time; only the crate and the
# argument digest vary, so the generated history looks like one build session.
_GENERATED_KEY = CacheKey(
    crate_name="serde",
    crate_origin="registry:serde@1.2.3",
    target="x86_64-unknown-linux-gnu",
    argument_digest="0" * 64,
    toolchain_identity=ToolchainIdentity(
        compiler_digest="a" * 64,
        rustup_toolchain_digest="b" * 64,
        cache_version="0.14.0",
        server_version="0.14.0",
    ),
)

__all__ = [
    "BENCH_SCHEMA",
    "CHUNK_BYTES",
    "GENERATED_RECORDS",
    "BenchmarkReport",
    "generate_segment",
    "measure",
    "peak_rows",
]


class _ChunkObserver:
    """Bytes-buffer proxy that records how many rows one chunk carried.

    The line reader owns its chunking, so the benchmark observes it rather
    than reimplementing it: one ``read`` is one chunk, and the rows that chunk
    completed are the rows that were buffered together.
    """

    def __init__(self, source: typ.BinaryIO) -> None:
        """Wrap ``source`` and start tracking its largest chunk."""
        self._source = source
        self.peak = 0

    def read(self, size: int = -1, /) -> bytes:
        """Read one chunk, counting the rows it completed.

        Returns
        -------
        bytes
            The chunk the reader will split.
        """
        chunk = self._source.read(size)
        self.peak = max(self.peak, peak_rows(chunk))
        return chunk


@dc.dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """What one benchmark run measured.

    Attributes
    ----------
    chunk_bytes : int
        The streaming chunk the reader consumed.
    source_bytes : int
        Logical bytes of the plain source.
    record_count : int
        Rows in the source, excluding its header.
    archive_bytes : int
        Bytes the XZ archive occupies.
    ratio : float
        Archive bytes divided by source bytes; lower is better.
    peak_rows : int
        Largest number of rows buffered at one moment.
    elapsed_seconds : float
        Wall time the compression pass took.
    peak_rss_bytes : int
        The process's peak resident set size after the pass.
    temp_bytes : int
        Scratch disk the pass used: the temporary archive's own size.
    peak_python_bytes : int
        Peak allocation Python's allocator served while the pass ran, or zero
        when the caller already had tracing active. liblzma allocates through
        that allocator, so this figure is dominated by the preset's encoder
        dictionary; it rises with the preset but not with the rows read.
        Unlike ``peak_rows``, it also detects a reader that starts retaining
        rows.
    """

    chunk_bytes: int
    source_bytes: int
    record_count: int
    archive_bytes: int
    ratio: float
    peak_rows: int
    elapsed_seconds: float = 0.0
    peak_rss_bytes: int = 0
    temp_bytes: int = 0
    peak_python_bytes: int = 0

    def as_dict(self) -> dict[str, object]:
        """Return the stable JSON fields of the report.

        Returns
        -------
        dict[str, object]
            A JSON-compatible mapping including the schema number.
        """
        return {
            "schema": BENCH_SCHEMA,
            "chunk_bytes": self.chunk_bytes,
            "source_bytes": self.source_bytes,
            "record_count": self.record_count,
            "archive_bytes": self.archive_bytes,
            "ratio": round(self.ratio, 6),
            "peak_rows": self.peak_rows,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "peak_rss_bytes": self.peak_rss_bytes,
            "temp_bytes": self.temp_bytes,
            "peak_python_bytes": self.peak_python_bytes,
        }


def measure(source: Path, *, preset: int = _PRESET) -> BenchmarkReport:
    """Compress one plain segment and report its sizes and streaming peak.

    Parameters
    ----------
    source : Path
        The sealed JSON Lines segment to read.
    preset : int
        The XZ preset the archiver would use.

    Returns
    -------
    BenchmarkReport
        The measured sizes, record count, ratio and peak buffering.
    """
    source_bytes = source.stat().st_size
    # The pass owns its trace only when it starts one. A caller already tracing
    # keeps its own peak: resetting it would hide the allocations it made
    # before this call, and reading it would attribute the caller's peak to the
    # pass, so the report leaves the figure at zero instead.
    owns_trace = not tracemalloc.is_tracing()
    if owns_trace:
        tracemalloc.start()
    started = time.perf_counter()
    try:
        with source.open("rb") as raw:
            rows, peak, archive_bytes = _compress(raw, preset)
        peak_python = tracemalloc.get_traced_memory()[1] if owns_trace else 0
    finally:
        if owns_trace:
            tracemalloc.stop()
    elapsed = time.perf_counter() - started
    ratio = archive_bytes / source_bytes if source_bytes else 0.0
    return BenchmarkReport(
        chunk_bytes=CHUNK_BYTES,
        source_bytes=source_bytes,
        record_count=rows,
        archive_bytes=archive_bytes,
        ratio=ratio,
        peak_rows=peak,
        elapsed_seconds=elapsed,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        temp_bytes=archive_bytes,
        peak_python_bytes=peak_python,
    )


def peak_rows(data: bytes) -> int:
    r"""Count complete rows held together when one chunk is split.

    The line reader appends a whole chunk to its buffer before it splits rows
    out, so every row whose newline falls inside that chunk can be pending at
    the same time. An unterminated tail is still streaming and holds nothing.

    Parameters
    ----------
    data : bytes
        One chunk, exactly as the reader received it.

    Returns
    -------
    int
        Complete rows in the chunk.

    Examples
    --------
    >>> peak_rows(b"a\nb\nc")
    2
    """
    return data.count(b"\n")


def _compress(raw: typ.BinaryIO, preset: int) -> tuple[int, int, int]:
    """Stream one source into XZ, observing the reader's chunks.

    Returns
    -------
    tuple[int, int, int]
        The record count excluding the header row, the peak rows held in one
        chunk, and the archive's physical size. A sealed segment always starts
        with a header row, which is not a record.
    """
    rows = 0
    observer = _ChunkObserver(raw)
    # The archive is written to an unnamed temporary file rather than an
    # in-memory buffer, so a large or poorly compressible segment cannot make
    # the benchmark's own memory grow with the archive it measures.
    with tempfile.TemporaryFile() as sink:
        with lzma.LZMAFile(
            sink, "wb", format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, preset=preset
        ) as archive:
            for row in LineStream(observer):
                archive.write(row)
                archive.write(b"\n")
                rows += 1
        archive_bytes = sink.seek(0, os.SEEK_END)
    return max(rows - 1, 0), observer.peak, archive_bytes


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Print one benchmark report for a generated or supplied segment.

    Parameters
    ----------
    argv : Sequence[str] | None
        Optionally one segment path; ``sys.argv[1:]`` when ``None``. Without a
        path a representative segment is generated in a temporary directory.

    Returns
    -------
    int
        Zero when a report was printed.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        print(json.dumps(measure(Path(arguments[0])).as_dict(), sort_keys=True))
        return 0
    with tempfile.TemporaryDirectory(prefix="buildlimits-archive-bench-") as scratch:
        report = measure(generate_segment(Path(scratch) / "segment-generated.jsonl"))
        print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


def generate_segment(path: Path) -> Path:
    """Write one decodable sealed segment: a header and attributed rows.

    The rows are built as ``OutcomeRecord`` values and written through the
    store's own encoder, so the generated input is exactly the document the
    archiver will one day meet. A handwritten row that the decoder rejects
    would benchmark a shape the archive workflow never sees.

    Returns
    -------
    Path
        The generated segment.

    Notes
    -----
    A generated row the encoder rejects raises ``ValueError``, which means the
    generator and the record vocabulary have drifted apart.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    header = encode_line(StoreHeader("0" * 64, _GENERATED_AT))
    with path.open("w", encoding="utf-8") as sink:
        sink.write(header)
        sink.write("\n")
        for index in range(GENERATED_RECORDS):
            sink.write(encode_line(_generated_record(index)))
            sink.write("\n")
    return path


def _generated_record(index: int) -> OutcomeRecord:
    """Build one representative row, alternating hit and miss on shared keys.

    Returns
    -------
    OutcomeRecord
        A cacheable row whose key repeats, so the generated history has the
        paired evidence a real store carries.
    """
    key = dc.replace(_GENERATED_KEY, argument_digest=f"{index // 2:064x}")
    label = "hit" if index % 2 == 0 else "miss"
    return OutcomeRecord(
        key=key,
        outcome=label,
        origin="registry",
        runtime_seconds=0.25 if label == "hit" else 1.5,
        observed_at=_GENERATED_AT,
        parser_class="cacheable",
        child_sighting="not_seen" if label == "hit" else "seen",
        child_samples=1,
    )


if __name__ == "__main__":
    raise SystemExit(main())
