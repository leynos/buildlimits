"""Bounded reads of the cache-outcome store document.

A store read trusts neither the file's size nor its rows. It opens the file
without following links, refuses a document larger than the store's bound, and
reports the exact byte length it decoded beside the rows, so a caller that
compares the two is comparing one state. The write side lives in
``outcome_store_io``, and the private-file primitives these reads build on live
in ``outcome_store_files``.
"""

import dataclasses as dc
import typing as typ

from buildlimits import outcome_records
from buildlimits.outcome_store_files import (
    OversizedFileError,
    read_private_bytes,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "StoreSnapshot",
    "read_private_bytes",
    "read_store",
    "read_store_snapshot",
]


@dc.dataclass(frozen=True, slots=True)
class StoreSnapshot:
    """One bounded read of a store, with the size of the bytes it decoded.

    Attributes
    ----------
    logical_bytes : int
        Bytes read from the file, header and every terminating newline
        included. A torn final fragment is part of the file's size even though
        it contributes no row.
    decoded : DecodedStore or RecordFailure
        Validated rows, or the closed reason the bytes could not be decoded.
    """

    logical_bytes: int
    decoded: outcome_records.DecodedStore | outcome_records.RecordFailure


def read_store(
    path: Path,
) -> outcome_records.DecodedStore | outcome_records.RecordFailure:
    """Read a private JSON Lines file; filesystem failures propagate as OSError.

    Returns
    -------
    DecodedStore or RecordFailure
        Validated store rows or the closed corruption/version result.

    """
    return read_store_snapshot(path).decoded


def read_store_snapshot(path: Path) -> StoreSnapshot:
    """Read a private JSON Lines file once, returning its bytes and its rows.

    Measuring the size separately would let a concurrent append land between
    the two calls, reporting the old size beside the new row count. One read
    keeps both figures describing the same document, which is what a
    diagnostic that compares them needs.

    Returns
    -------
    StoreSnapshot
        The decoded rows or closed failure, with the exact byte length read.
        A filesystem failure propagates as ``OSError`` rather than becoming a
        snapshot: an unreadable store is not an empty one.
    """
    try:
        document = read_private_bytes(path, limit=outcome_records.MAX_STORE_BYTES)
    except OversizedFileError as oversized:
        # The rows are untrusted, but the refused file's size is still a fact.
        # Measuring the pathname again could describe a replacement the writer
        # installed after the refusal, pairing its size with this file's
        # corruption result.
        return StoreSnapshot(oversized.size, outcome_records.RecordFailure("corrupt"))
    try:
        complete_document = document[: document.rfind(b"\n") + 1]
        # An interrupted append can end inside a multibyte character. Discard
        # that unterminated byte sequence before strict UTF-8 decoding.
        decoded = outcome_records.decode_lines(complete_document.decode("utf-8"))
    except UnicodeError:
        decoded = outcome_records.RecordFailure("corrupt")
    return StoreSnapshot(len(document), decoded)
