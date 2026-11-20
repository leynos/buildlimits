"""Sidecar records that describe one verified XZ archive or sealed source.

A sidecar is the inventory's only source of truth for an archive's logical
size, digest and observation span. It is written after the archive is durable
and read through the same strict validation in every process, so a sidecar that
disagrees with its sequence is reported as corrupt rather than trusted.
"""

import dataclasses as dc
import json
import typing as typ

from buildlimits.outcome_store_files import OversizedFileError, read_private_bytes
from buildlimits.utc_text import optional_utc_moment, optional_utc_text

if typ.TYPE_CHECKING:
    import datetime as dt
    from pathlib import Path

SIDECAR_SCHEMA = 1
_SHA256_HEX_LENGTH = 64
# A sidecar holds one small object. The bound stops an oversized or hostile
# file in the archive directory from being read into memory.
_MAX_SIDECAR_BYTES = 64 * 1024

__all__ = [
    "SIDECAR_SCHEMA",
    "Sidecar",
    "metadata_fields",
    "read_mapping",
    "read_sidecar",
    "required_int",
    "required_text",
]


@dc.dataclass(frozen=True, slots=True)
class Sidecar:
    """The verified facts recorded beside one XZ archive.

    Attributes
    ----------
    sequence : int
        The segment sequence.
    salt_id : str
        The record-domain identifier preserved from the source header.
    record_count : int
        Non-header rows in the archive.
    logical_bytes : int
        Uncompressed bytes, including newlines and the header.
    physical_bytes : int
        Bytes the XZ file occupies.
    sha256 : str
        Digest of the uncompressed content.
    first_observed_at : datetime | None
        Earliest row observation, or None when the segment held no rows.
    last_observed_at : datetime | None
        Latest row observation, or None when the segment held no rows.
    archived_at : datetime
        When the archive was published.
    xz_preset : int
        The compression preset used.
    had_torn_tail : bool
        Whether an incomplete final row was dropped.
    """

    sequence: int
    salt_id: str
    record_count: int
    logical_bytes: int
    physical_bytes: int
    sha256: str
    first_observed_at: dt.datetime | None
    last_observed_at: dt.datetime | None
    archived_at: dt.datetime
    xz_preset: int
    had_torn_tail: bool

    def as_dict(self) -> dict[str, object]:
        """Return the stable sidecar JSON fields.

        Returns
        -------
        dict[str, object]
            A JSON-compatible mapping with the schema number.
        """
        return {
            "schema": SIDECAR_SCHEMA,
            "sequence": self.sequence,
            "format": "buildlimits-cache-outcomes",
            "format_version": 1,
            "salt_id": self.salt_id,
            "record_count": self.record_count,
            "logical_bytes": self.logical_bytes,
            "physical_bytes": self.physical_bytes,
            "sha256": self.sha256,
            "first_observed_at": optional_utc_text(self.first_observed_at),
            "last_observed_at": optional_utc_text(self.last_observed_at),
            "archived_at": optional_utc_text(self.archived_at),
            "xz_preset": self.xz_preset,
            "xz_check": "crc64",
            "had_torn_tail": self.had_torn_tail,
        }


def read_mapping(path: Path) -> dict[str, object] | None:
    """Read a bounded JSON object, or None when it is absent or invalid.

    Returns
    -------
    dict[str, object] | None
        The mapping, or None.
    """
    try:
        raw = read_private_bytes(path, limit=_MAX_SIDECAR_BYTES)
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError, TypeError, RecursionError:
        return None
    return document if isinstance(document, dict) else None


def read_sidecar(path: Path, sequence: int) -> Sidecar | typ.Literal["corrupt"] | None:
    """Read and validate one sidecar, distinguishing absent from corrupt.

    Returns
    -------
    Sidecar | Literal["corrupt"] | None
        The sidecar, the corruption marker, or None when it is absent.
    """
    try:
        raw = read_private_bytes(path, limit=_MAX_SIDECAR_BYTES)
    except FileNotFoundError:
        return None
    except OSError:
        return "corrupt"
    try:
        document = json.loads(raw)
    except ValueError, TypeError, RecursionError:
        return "corrupt"
    if not isinstance(document, dict):
        return "corrupt"
    return _sidecar_from_mapping(document, sequence)


def metadata_fields(metadata: dict[str, object] | None) -> dict[str, object]:
    """Translate one writer metadata mapping into entry fields.

    Returns
    -------
    dict[str, object]
        Fields to merge into a sealed entry; empty when metadata is unusable.
    """
    if metadata is None:
        return {}
    try:
        first_observed_at = _optional_moment(metadata, "first_observed_at")
        last_observed_at = _optional_moment(metadata, "last_observed_at")
        if _spans_backwards(first_observed_at, last_observed_at):
            # The age planner removes a sealed source from its span alone. An
            # inverted span would claim the source is older than it is, so a
            # description that contradicts itself is withheld.
            return {}
        return {
            "record_count": required_int(metadata, "record_count"),
            "first_observed_at": first_observed_at,
            "last_observed_at": last_observed_at,
            "salt_id": required_text(metadata, "salt_id"),
            "has_metadata": True,
        }
    except TypeError, ValueError:
        return {}


def _sidecar_from_mapping(
    document: dict[str, object], sequence: int
) -> Sidecar | typ.Literal["corrupt"]:
    """Validate one sidecar mapping against its sequence.

    Returns
    -------
    Sidecar | Literal["corrupt"]
        The typed sidecar or the corruption marker.
    """
    try:
        if document.get("schema") != SIDECAR_SCHEMA:
            return "corrupt"
        if document.get("sequence") != sequence:
            return "corrupt"
        archived_at = optional_utc_moment(
            document.get("archived_at"),
            label="sidecar timestamp",
            message="a sidecar timestamp must be text",
        )
        if archived_at is None:
            return "corrupt"
        sha256 = required_text(document, "sha256")
        is_digest = len(sha256) == _SHA256_HEX_LENGTH and all(
            char in "0123456789abcdef" for char in sha256
        )
        first_observed_at = _optional_moment(document, "first_observed_at")
        last_observed_at = _optional_moment(document, "last_observed_at")
        # A digest that cannot name its bytes leaves the sidecar untrusted,
        # and so does a span that ends before it starts: the age planner
        # deletes a trusted sidecar's archive from that span alone, so an
        # inverted one could remove rows still inside the retention window.
        if not is_digest or _spans_backwards(first_observed_at, last_observed_at):
            return "corrupt"
        return Sidecar(
            sequence=sequence,
            salt_id=required_text(document, "salt_id"),
            record_count=required_int(document, "record_count"),
            logical_bytes=required_int(document, "logical_bytes"),
            physical_bytes=required_int(document, "physical_bytes"),
            sha256=sha256,
            first_observed_at=first_observed_at,
            last_observed_at=last_observed_at,
            archived_at=archived_at,
            xz_preset=required_int(document, "xz_preset"),
            had_torn_tail=document.get("had_torn_tail") is True,
        )
    except TypeError, ValueError:
        return "corrupt"


def required_text(document: dict[str, object], name: str) -> str:
    """Read one required non-empty string field.

    Parameters
    ----------
    document : dict[str, object]
        The mapping to read.
    name : str
        The field name.

    Returns
    -------
    str
        The field's non-empty text.

    Raises
    ------
    TypeError
        If the field is not text.
    ValueError
        If the field is missing or empty.
    """
    value = document.get(name)
    if not isinstance(value, str):
        raise TypeError(name)
    if not value.strip():
        raise ValueError(name)
    return value


def required_int(document: dict[str, object], name: str) -> int:
    """Read one required non-negative integer field.

    Parameters
    ----------
    document : dict[str, object]
        The mapping to read.
    name : str
        The field name.

    Returns
    -------
    int
        The field's non-negative integer value.

    Raises
    ------
    TypeError
        If the field is missing, boolean or not an integer.
    ValueError
        If the field is negative.
    """
    value = document.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(name)
    if value < 0:
        raise ValueError(name)
    return value


def _spans_backwards(first: dt.datetime | None, last: dt.datetime | None) -> bool:
    """Whether an observation span ends before it starts.

    Returns
    -------
    bool
        True when both ends are known and the last precedes the first.
    """
    if first is None or last is None:
        return False
    return first > last


def _optional_moment(document: dict[str, object], name: str) -> dt.datetime | None:
    """Read one optional timestamp field.

    Returns
    -------
    datetime | None
        The parsed UTC moment, or None when the field is absent.
    """
    return optional_utc_moment(
        document.get(name),
        label=name,
        message=f"{name} must be text",
    )
