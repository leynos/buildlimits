"""Composing a byte allowance with the count and age rules of one trim.

A byte allowance is compared with the document that survives every other rule,
and only a pass over the source can measure what those rules drop. The pass
also has to price a plain rewrite's terminating newline and an undecodable
torn fragment, because neither is part of the input size a caller measured.
Keeping that arithmetic here leaves the archive writer and the in-place sealed
writer sharing one definition of what the written document will cost.

Working memory stays bounded by one row: the pass streams the source exactly as
the writers do.
"""

import dataclasses as dc
import os
import typing as typ

from buildlimits.outcome_archive_fs import open_private_regular
from buildlimits.outcome_archive_rows import DocumentReader, iter_records
from buildlimits.outcome_archive_trim import Trim, policy_drops
from buildlimits.outcome_record_stream import LineStream

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "PolicyDrops",
    "composed_trim",
    "measure_policy_drops",
    "policy_dropped_bytes",
]


def composed_trim(source_path: Path, trim: Trim, *, is_compressed: bool) -> Trim:
    """Return the trim the writing pass needs, measuring policy drops first.

    A byte allowance must be compared with the document that survives the
    count and age rules, and only a whole pass over the source can measure
    what those rules drop. That pass is skipped when the allowance cannot
    compose with either rule, so an ordinary rewrite still reads its source
    once before the verification pass. The measurement propagates the
    ``SegmentFaultError`` its reader reports when a row or header in the source
    is invalid.

    The plain and compressed rewrites share this measurement, so a sealed
    source trimmed in place and an archive recompressed under the same policy
    drop the same rows.

    Parameters
    ----------
    source_path : Path
        The plain or compressed document the writing pass will read.
    trim : Trim
        The prefix policy to resolve.
    is_compressed : bool
        Whether the source is an XZ archive.

    Returns
    -------
    Trim
        The trim, with ``policy_bytes`` measured when the byte allowance
        composes with a count or age rule, and with the source size reduced by
        a final fragment the reader would drop.
    """
    if trim.allowed_bytes is None:
        return trim
    descriptor, _metadata = open_private_regular(
        source_path, os.O_RDONLY, enforce_mode=False
    )
    with os.fdopen(descriptor, "rb") as source:
        drops = measure_policy_drops(
            LineStream(source, is_compressed=is_compressed), trim
        )
    if not is_compressed:
        # The plain writer terminates every kept row, and the reader drops an
        # undecodable fragment. Normalizing the source size by both keeps the
        # allowance comparing like with like: an unterminated final row is one
        # byte larger once written, while a dropped fragment is never written
        # at all. Without the newline term, a source exactly at its allowance
        # would be written one byte over it.
        adjusted = trim.source_bytes + drops.restored_newline_bytes - drops.tail_bytes
        if adjusted != trim.source_bytes:
            trim = dc.replace(trim, source_bytes=max(adjusted, 0))
    composes = trim.before is not None or trim.records != 0
    return dc.replace(trim, policy_bytes=drops.dropped_bytes if composes else 0)


@dc.dataclass(frozen=True, slots=True)
class PolicyDrops:
    """What one measurement pass over a source found.

    Attributes
    ----------
    dropped_bytes : int
        Bytes, including newlines, of the record rows the count and age rules
        drop.
    tail_bytes : int
        Bytes of an undecodable unterminated fragment the reader dropped. Those
        bytes are never written, whatever allowance the trim carries.
    restored_newline_bytes : int
        The newline the writer restores after a decodable final row that the
        source left unterminated: one byte, or zero when the source ended with
        a newline or in a dropped fragment.
    """

    dropped_bytes: int
    tail_bytes: int
    restored_newline_bytes: int = 0


def measure_policy_drops(stream: LineStream, trim: Trim) -> PolicyDrops:
    """Measure one source for the bytes a trim drops.

    A byte allowance is compared with the document that results after every
    other rule has been applied, and a row the count or age rule drops after
    the byte suffix has started is absent from that document too. Deciding the
    suffix before seeing those rows would discard a row that fits, so this
    pass measures them first. The same pass reports a final fragment the reader
    drops, because no allowance can keep bytes the writing pass never emits.
    Rows are still read one at a time, so the memory bound is unchanged.

    Parameters
    ----------
    stream : LineStream
        The uncompressed source rows.
    trim : Trim
        The policy whose ``records`` and ``before`` rules apply.

    Returns
    -------
    PolicyDrops
        The policy-dropped bytes, the dropped fragment's bytes, and the newline
        a plain rewrite would restore.

    Examples
    --------
    >>> import datetime as dt, io
    >>> from buildlimits.outcome_record_encode import encode_line
    >>> from buildlimits.outcome_record_format import StoreHeader
    >>> header = StoreHeader("s", dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    >>> text = encode_line(header) + chr(10)
    >>> measure_policy_drops(LineStream(io.BytesIO(text.encode())), Trim())
    PolicyDrops(dropped_bytes=0, tail_bytes=0, restored_newline_bytes=0)
    """
    reader = DocumentReader(stream)
    dropped = 0
    for seen, row in enumerate(iter_records(reader)):
        if policy_drops(row.record.observed_at, trim, seen=seen):
            dropped += len(row.raw) + 1
    return PolicyDrops(
        dropped_bytes=dropped,
        tail_bytes=reader.dropped_tail_bytes,
        restored_newline_bytes=1 if reader.missing_final_newline else 0,
    )


def policy_dropped_bytes(stream: LineStream, trim: Trim) -> int:
    """Return only the policy-dropped bytes of one measurement pass.

    Returns
    -------
    int
        Bytes, including newlines, of the record rows the count and age rules
        drop.
    """
    return measure_policy_drops(stream, trim).dropped_bytes
