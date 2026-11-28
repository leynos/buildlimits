"""Durable small documents written beside a cache-outcome store.

The writer's sequence counter and each sealed segment's metadata are single
short values that must be replaced atomically. They share the private
temporary-file and directory-sync primitives in ``outcome_store_io``; keeping
their JSON shapes here leaves that module to the store file itself.
"""

import dataclasses as dc
import json
import typing as typ

from buildlimits.outcome_store_io import (
    fsync_directory,
    replace_temp,
    write_private_temporary,
)
from buildlimits.utc_text import optional_utc_text, utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreRecord

__all__ = [
    "METADATA_SCHEMA",
    "SealedRows",
    "sealed_metadata",
    "sealed_metadata_document",
    "write_metadata",
    "write_sequence",
]

METADATA_SCHEMA = 1
"""The sealed-metadata schema the writer publishes.

The archiver rewrites a source's counts and span after an in-place retention
trim, and it must write the document the writer's inventory reads. The schema
number is named here so both processes share one definition rather than
carrying a copy each.
"""


def write_sequence(path: Path, value: int) -> None:
    """Atomically store the next sealed sequence number.

    The counter is written before a segment is moved, so a crash leaves a gap
    the inventory reports rather than a sequence two segments can share.
    Filesystem failures propagate as ``OSError``.
    """
    _replace_document(path, f"{value}\n".encode("ascii"))


def write_metadata(path: Path, document: cabc.Mapping[str, object]) -> None:
    """Atomically store one JSON metadata document beside a sealed source.

    Filesystem failures propagate as ``OSError``.
    """
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    _replace_document(path, encoded + b"\n")


def _replace_document(path: Path, data: bytes) -> None:
    """Write bytes through one private temporary file and replace ``path``."""
    replace_temp(write_private_temporary(path, data), path)
    fsync_directory(path.parent)


@dc.dataclass(frozen=True, slots=True)
class SealedRows:
    """What one sealed document holds, as its metadata records it.

    Attributes
    ----------
    record_count : int
        Non-header rows the document held when it was described.
    first_observed_at : datetime | None
        Earliest row observation, or None when the document held no rows.
    last_observed_at : datetime | None
        Latest row observation, or None when the document held no rows.
    """

    record_count: int
    first_observed_at: dt.datetime | None
    last_observed_at: dt.datetime | None


def sealed_metadata(
    records: list[StoreRecord], sequence: int, salt_id: str, now: dt.datetime
) -> dict[str, object]:
    """Describe the rows about to be sealed for the archiver and reports.

    Returns
    -------
    dict[str, object]
        Counts, salt identity and observed range, with no private values.
    """
    observed = [record.observed_at for record in records]
    return sealed_metadata_document(
        sequence=sequence,
        salt_id=salt_id,
        rows=SealedRows(
            record_count=len(observed),
            first_observed_at=min(observed, default=None),
            last_observed_at=max(observed, default=None),
        ),
        sealed_at=now,
    )


def sealed_metadata_document(
    *,
    sequence: int,
    salt_id: str,
    rows: SealedRows,
    sealed_at: dt.datetime,
) -> dict[str, object]:
    """Build the metadata document one sealed source is described by.

    The writer and the archiver both publish this document, so its field names,
    schema number and timestamp encoding live in one place. An in-place trim
    keeps the original ``sealed_at`` so a source's age is not reset by
    maintenance that only dropped expired rows.

    Parameters
    ----------
    sequence : int
        The segment sequence the document describes.
    salt_id : str
        The salt identity the source's rows were written under.
    rows : SealedRows
        The retained count and observation span the source holds.
    sealed_at : datetime
        When the source was sealed, kept across an in-place trim.

    Returns
    -------
    dict[str, object]
        Counts, salt identity and observed range, with no private values.

    Raises
    ------
    ValueError
        If ``sealed_at`` carries no offset. The formatter refuses it too, but
        the check belongs at this public boundary: a naive value would be read
        as the host's local time, so the same seal would be stored differently
        on two machines.

    Examples
    --------
    >>> import datetime as dt
    >>> document = sealed_metadata_document(
    ...     sequence=3,
    ...     salt_id="salt",
    ...     rows=SealedRows(0, None, None),
    ...     sealed_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    ... )
    >>> document["schema"], document["record_count"]
    (1, 0)
    """
    if sealed_at.utcoffset() is None:
        msg = "a sealed source's seal time must carry an offset"
        raise ValueError(msg)
    return {
        "schema": METADATA_SCHEMA,
        "sequence": sequence,
        "salt_id": salt_id,
        "record_count": rows.record_count,
        "first_observed_at": optional_utc_text(rows.first_observed_at),
        "last_observed_at": optional_utc_text(rows.last_observed_at),
        "sealed_at": utc_text(sealed_at),
    }
