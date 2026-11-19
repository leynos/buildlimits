"""Bounded chunk-based line splitting and XZ decoding for outcome segments.

Nothing here reads a whole segment or a whole history into memory. A caller
supplies a binary stream, iterates rows in fixed-size chunks, and reads the
stream's counters afterwards. The XZ reader insists on reaching the declared
end of the stream, so a truncated file or trailing garbage is a closed fault
rather than a silently short history.
"""

import hashlib
import lzma
import typing as typ

from buildlimits.outcome_record_format import MAX_RECORD_BYTES

CHUNK_BYTES = 64 * 1024
_XZ_PADDING = 4

if typ.TYPE_CHECKING:
    import collections.abc as cabc

type StreamReason = typ.Literal[
    "line_too_long",
    "truncated_xz",
    "trailing_data",
    "bad_xz",
]


class Readable(typ.Protocol):
    """A binary source the line reader consumes in fixed-size chunks."""

    def read(self, size: int = -1) -> bytes:
        """Read up to ``size`` bytes, returning an empty value at end."""
        ...


class StreamFaultError(Exception):
    """A bounded streaming failure with a closed reason.

    Parameters
    ----------
    reason : StreamReason
        The fault classification callers report without raw exception text.
    """

    def __init__(self, reason: StreamReason) -> None:
        """Retain the closed reason as the exception message."""
        super().__init__(reason)
        self.reason = reason


class LineStream:
    r"""Iterate newline-delimited rows with bounded buffering and hashing.

    The stream hashes and counts the decoded bytes it yields, not the source
    file's physical bytes, so the same digest describes a plain JSONL segment
    and the decompressed content of its XZ archive.

    Parameters
    ----------
    source : BinaryIO
        An open binary stream. The caller owns and closes it.
    is_compressed : bool, default=False
        Whether the source holds an XZ stream to decompress.
    chunk_bytes : int, default=CHUNK_BYTES
        Fixed read size; peak buffering is one chunk plus one row.
    max_line_bytes : int, default=MAX_RECORD_BYTES
        Longest permitted row, excluding its newline.

    Attributes
    ----------
    bytes_read : int
        Decoded bytes consumed so far, including newlines.
    is_terminated : bool
        Whether the final row ended with a newline once iteration finished.
    is_verified : bool
        Whether a compressed stream reached its declared end and checksum.

    Examples
    --------
    >>> import io
    >>> stream = LineStream(io.BytesIO(b"one\ntwo\n"))
    >>> list(stream)
    [b'one', b'two']
    >>> stream.is_terminated, stream.bytes_read
    (True, 8)
    """

    def __init__(
        self,
        source: Readable,
        *,
        is_compressed: bool = False,
        chunk_bytes: int = CHUNK_BYTES,
        max_line_bytes: int = MAX_RECORD_BYTES,
    ) -> None:
        """Bind one source without reading it yet.

        Raises
        ------
        ValueError
            If the chunk or row bound is not positive.
        """
        if chunk_bytes < 1 or max_line_bytes < 1:
            msg = "stream bounds must be positive"
            raise ValueError(msg)
        self._source = source
        self._is_compressed = is_compressed
        self._chunk_bytes = chunk_bytes
        self._max_line_bytes = max_line_bytes
        self._hasher = hashlib.sha256()
        self.bytes_read = 0
        self.is_terminated = True
        self.is_verified = not is_compressed

    def __iter__(self) -> cabc.Iterator[bytes]:
        """Iterate rows without their newline in document order.

        Yields
        ------
        bytes
            The decoded rows.

        Raises
        ------
        StreamFaultError
            If a row exceeds its bound or the XZ stream is unusable.
        """
        buffer = b""
        for chunk in self._chunks():
            self._hasher.update(chunk)
            self.bytes_read += len(chunk)
            buffer += chunk
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                line, buffer = buffer[:newline], buffer[newline + 1 :]
                yield self._bounded(line)
            if len(buffer) > self._max_line_bytes:
                raise StreamFaultError("line_too_long")
        if buffer:
            self.is_terminated = False
            yield self._bounded(buffer)

    @property
    def sha256_hexdigest(self) -> str:
        """Hex digest of the decoded bytes consumed so far.

        Returns
        -------
        str
            A lowercase SHA-256 digest.
        """
        return self._hasher.hexdigest()

    def _bounded(self, line: bytes) -> bytes:
        """Validate one row against its byte bound.

        Parameters
        ----------
        line : bytes
            One candidate row without its newline.

        Returns
        -------
        bytes
            The same row when it is within the bound.

        Raises
        ------
        StreamFaultError
            If the row exceeds ``max_line_bytes``.
        """
        if len(line) > self._max_line_bytes:
            raise StreamFaultError("line_too_long")
        return line

    def _chunks(self) -> cabc.Iterator[bytes]:
        """Yield decoded chunks from a plain or XZ-compressed source.

        Returns
        -------
        collections.abc.Iterator[bytes]
            Decoded chunks in order.
        """
        if self._is_compressed:
            return self._xz_chunks()
        return self._plain_chunks()

    def _plain_chunks(self) -> cabc.Iterator[bytes]:
        """Iterate fixed-size chunks from an uncompressed source.

        Yields
        ------
        bytes
            Each non-empty chunk until the source is exhausted.
        """
        while True:
            chunk = self._source.read(self._chunk_bytes)
            if not chunk:
                return
            yield chunk

    def _xz_chunks(self) -> cabc.Iterator[bytes]:
        """Iterate chunks decompressed from one concatenated XZ stream.

        Yields
        ------
        bytes
            Decompressed chunks in order.

        Raises
        ------
        StreamFaultError
            If the stream is corrupt, truncated or followed by other data.
        """
        decompressor = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
        while True:
            chunk = self._source.read(self._chunk_bytes)
            if not chunk:
                if not decompressor.eof:
                    raise StreamFaultError("truncated_xz")
                self.is_verified = True
                return
            data = self._feed(decompressor, chunk)
            if data:
                yield data
            if decompressor.eof:
                self._require_trailing_padding(decompressor)
                self.is_verified = True
                return

    @staticmethod
    def _feed(decompressor: lzma.LZMADecompressor, chunk: bytes) -> bytes:
        """Feed one source chunk and drain any input the decoder retained.

        Parameters
        ----------
        decompressor : lzma.LZMADecompressor
            The decoder for the current XZ stream.
        chunk : bytes
            One source chunk.

        Returns
        -------
        bytes
            Every byte decompressed from the chunk.

        Raises
        ------
        StreamFaultError
            If the XZ data is malformed.
        """
        try:
            output = [decompressor.decompress(chunk)]
            while not decompressor.needs_input and not decompressor.eof:
                more = decompressor.decompress(b"")
                if not more:
                    break
                output.append(more)
        except lzma.LZMAError as error:
            raise StreamFaultError("bad_xz") from error
        return b"".join(output)

    def _require_trailing_padding(self, decompressor: lzma.LZMADecompressor) -> None:
        """Reject trailing data that is not XZ stream padding.

        Parameters
        ----------
        decompressor : lzma.LZMADecompressor
            The decoder that just reached its declared end.

        Raises
        ------
        StreamFaultError
            If the bytes after the stream end are not NUL padding.
        """
        trailing = decompressor.unused_data + self._source.read()
        if not trailing:
            return
        is_padded = len(trailing) % _XZ_PADDING == 0
        if not is_padded or trailing.strip(b"\x00"):
            raise StreamFaultError("trailing_data")
