"""Bounded line streaming and its closed XZ fault reasons.

Every archive and history read goes through this reader, so a truncated file,
trailing garbage or an overlong row must be a named fault rather than a
shortened history. The tests also pin the decoded-byte digest, because that is
the value an archive is verified against.
"""

import hashlib
import io
import lzma

import pytest

from buildlimits.outcome_record_stream import CHUNK_BYTES, LineStream, StreamFaultError

_DATA = b"one\ntwo\nthree\n"


def _row_reason(stream: LineStream) -> str:
    """Iterate a stream to its end and return the fault it raised.

    Returns
    -------
    str
        The closed reason, or ``"ok"`` when the stream was read completely.
    """
    try:
        list(stream)
    except StreamFaultError as fault:
        return fault.reason
    return "ok"


def _archive(data: bytes) -> bytes:
    """Compress ``data`` as one XZ stream.

    Returns
    -------
    bytes
        The XZ archive.
    """
    return lzma.compress(data, format=lzma.FORMAT_XZ)


def test_rows_are_split_across_chunk_boundaries() -> None:
    """A two-byte chunk still yields whole rows in order."""
    stream = LineStream(io.BytesIO(_DATA), chunk_bytes=2)

    assert list(stream) == [b"one", b"two", b"three"], "rows are reassembled"
    assert stream.is_terminated, "the final row ended with a newline"
    assert stream.bytes_read == len(_DATA), "every decoded byte is counted"
    assert stream.sha256_hexdigest == hashlib.sha256(_DATA).hexdigest(), (
        "the digest describes the decoded content"
    )


def test_an_unterminated_final_row_is_yielded_and_marked() -> None:
    """A crash-truncated tail is readable but the stream says it was torn."""
    stream = LineStream(io.BytesIO(b"one\ntw"))

    assert list(stream) == [b"one", b"tw"], "the final fragment is still a row"
    assert not stream.is_terminated, "the caller can tell the row was unterminated"


@pytest.mark.parametrize(
    "data",
    [b"123456\n", b"123456"],
    ids=["terminated", "unterminated"],
)
def test_a_row_over_its_bound_is_a_fault(data: bytes) -> None:
    """Neither a complete nor a pending row may exceed the configured bound."""
    stream = LineStream(io.BytesIO(data), max_line_bytes=5)

    assert _row_reason(stream) == "line_too_long", "the fault names the row bound"


@pytest.mark.parametrize(
    ("chunk_bytes", "max_line_bytes"),
    [(0, 10), (10, 0)],
    ids=["chunk", "row"],
)
def test_stream_bounds_must_be_positive(chunk_bytes: int, max_line_bytes: int) -> None:
    """A zero bound would make the reader unable to make progress."""
    with pytest.raises(ValueError, match="bounds must be positive"):
        LineStream(
            io.BytesIO(b""),
            chunk_bytes=chunk_bytes,
            max_line_bytes=max_line_bytes,
        )


def test_an_xz_stream_yields_the_same_rows_and_digest() -> None:
    """Decompressed rows hash exactly like their plain source."""
    stream = LineStream(io.BytesIO(_archive(_DATA)), is_compressed=True)

    assert list(stream) == [b"one", b"two", b"three"], "rows survive compression"
    assert stream.is_verified, "the declared stream end was reached"
    assert stream.bytes_read == len(_DATA), "decoded bytes are counted, not XZ bytes"
    assert stream.sha256_hexdigest == hashlib.sha256(_DATA).hexdigest(), (
        "a plain and compressed segment share one digest"
    )


def test_an_xz_stream_read_in_small_chunks_is_verified() -> None:
    """Chunking never changes the decoded result or the verification flag."""
    stream = LineStream(io.BytesIO(_archive(_DATA)), is_compressed=True, chunk_bytes=3)

    assert list(stream) == [b"one", b"two", b"three"], "chunks are joined"
    assert stream.is_verified, "the stream end is still reached"


def test_a_truncated_xz_stream_is_a_fault() -> None:
    """A file shorter than its declared stream never counts as complete."""
    stream = LineStream(io.BytesIO(_archive(_DATA)[:-8]), is_compressed=True)

    assert _row_reason(stream) == "truncated_xz", "the fault names truncation"
    assert not stream.is_verified, "a truncated stream is never verified"


def test_malformed_xz_data_is_a_fault() -> None:
    """Bytes that are not XZ at all are rejected before any row is trusted."""
    stream = LineStream(io.BytesIO(b"not xz at all"), is_compressed=True)

    assert _row_reason(stream) == "bad_xz", "the fault names the encoding"


def test_trailing_data_after_the_stream_is_a_fault() -> None:
    """Garbage appended to a valid archive cannot be silently ignored."""
    stream = LineStream(io.BytesIO(_archive(_DATA) + b"junkjunk"), is_compressed=True)

    assert _row_reason(stream) == "trailing_data", "the fault names the extra bytes"


@pytest.mark.parametrize("padding", [b"\x00" * 4, b"\x00" * 8], ids=["four", "eight"])
def test_xz_stream_padding_is_accepted(padding: bytes) -> None:
    """The XZ format permits NUL padding in four-byte units."""
    stream = LineStream(io.BytesIO(_archive(_DATA) + padding), is_compressed=True)

    assert list(stream) == [b"one", b"two", b"three"], "padding is not history"
    assert stream.is_verified, "padding after a complete stream is legal"


@pytest.mark.parametrize("padding", [b"\x00", b"\x00" * 3], ids=["one", "three"])
def test_misaligned_padding_is_trailing_data(padding: bytes) -> None:
    """NUL bytes that do not complete a four-byte unit are not padding."""
    stream = LineStream(io.BytesIO(_archive(_DATA) + padding), is_compressed=True)

    assert _row_reason(stream) == "trailing_data", "the format's unit is enforced"


def test_the_default_chunk_is_the_documented_bound() -> None:
    """The reader's chunk is the value the benchmark reports."""
    assert CHUNK_BYTES == 64 * 1024, "the chunk is one fixed block"
