"""Measure archiving one segment without reading it into memory.

The archiver's promise is that a 15 GB retained history is handled in bounded
memory. This benchmark states the size of each input it reads, the physical and
logical size of the result, and the largest number of rows the streaming reader
ever buffers, so a regression in memory behaviour is visible in a report rather
than inferred from a stopwatch.

Run ``make outcome-archive-bench`` to print one report for a generated
segment; a caller with a real segment passes its path instead.
"""

import dataclasses as dc
import io
import json
import lzma
import tempfile
import typing as typ
from pathlib import Path

from buildlimits.outcome_record_stream import CHUNK_BYTES, LineStream

if typ.TYPE_CHECKING:
    import collections.abc as cabc

BENCH_SCHEMA = 1
_PRESET = 6

__all__ = [
    "BENCH_SCHEMA",
    "CHUNK_BYTES",
    "BenchmarkReport",
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
    """

    chunk_bytes: int
    source_bytes: int
    record_count: int
    archive_bytes: int
    ratio: float
    peak_rows: int

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
    with source.open("rb") as raw:
        rows, peak, archive_bytes = _compress(raw, preset)
    ratio = archive_bytes / source_bytes if source_bytes else 0.0
    return BenchmarkReport(
        chunk_bytes=CHUNK_BYTES,
        source_bytes=source_bytes,
        record_count=rows,
        archive_bytes=archive_bytes,
        ratio=ratio,
        peak_rows=peak,
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
    >>> peak_rows(b"a\\nb\\nc")
    2
    """
    return data.count(b"\n")


def _compress(raw: typ.BinaryIO, preset: int) -> tuple[int, int, int]:
    """Stream one source into XZ, observing the reader's chunks.

    Returns
    -------
    tuple[int, int, int]
        The record count excluding the header, the peak rows held in one
        chunk, and the archive's physical size.
    """
    rows = 0
    observer = _ChunkObserver(raw)
    sink = io.BytesIO()
    with lzma.LZMAFile(
        sink, "wb", format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, preset=preset
    ) as archive:
        for row in LineStream(observer):
            archive.write(row)
            archive.write(b"\n")
            rows += 1
    return max(rows - 1, 0), observer.peak, len(sink.getvalue())


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Print one benchmark report for a generated or supplied segment.

    Parameters
    ----------
    argv : Sequence[str] | None
        Optionally one segment path; otherwise a segment is generated.

    Returns
    -------
    int
        Zero when a report was printed.
    """
    arguments = list(argv or [])
    with tempfile.TemporaryDirectory(prefix="buildlimits-archive-bench-") as scratch:
        source = Path(arguments[0]) if arguments else _generated(Path(scratch))
        report = measure(source)
        print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


def _generated(directory: Path) -> Path:
    """Write a representative segment of attributed rows.

    Returns
    -------
    Path
        The generated segment.
    """
    row = json.dumps({
        "kind": "outcome",
        "key": {"crate_name": "serde", "crate_origin": "registry:serde@1.2.3"},
        "outcome": "hit",
        "origin": "registry",
        "runtime_seconds": 0.25,
        "observed_at": "2026-11-19T00:00:00Z",
    })
    path = directory / "segment-generated.jsonl"
    with path.open("w", encoding="utf-8") as sink:
        for _ in range(5_000):
            sink.write(row)
            sink.write("\n")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
