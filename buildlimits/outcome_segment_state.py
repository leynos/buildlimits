"""Read the two small state files the segmented store keeps.

Both files are advisory: a missing or damaged counter or retention record is
reported as unknown rather than trusted, so a scan or a run still works after a
crash halfway through publishing one of them. The retention record's unknown
states are named separately, because "never recorded" is a fresh store while
"recorded and damaged" is state the archiver must stop trusting.
"""

import dataclasses as dc
import json
import os
import stat
import typing as typ

from buildlimits.outcome_store_files import OversizedFileError, read_private_bytes

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_segments import SegmentLayout

# Why the retention record carries no usable floor.
type FloorState = typ.Literal["recorded", "missing", "unreadable", "invalid"]

_MAX_SEQUENCE_TEXT = 32
# The retention record is a handful of JSON fields; anything larger is not
# state this code wrote and must not be read into memory.
_MAX_STATE_BYTES = 4096

__all__ = ["FloorState", "RetentionFloor", "read_retention_floor", "read_sequence"]


@dc.dataclass(frozen=True, slots=True)
class RetentionFloor:
    """The floor the last successful run recorded, or why none was read.

    Attributes
    ----------
    sequence : int | None
        The lowest retained sequence, or None when no floor could be read.
    state : FloorState
        ``recorded`` for a floor this store's archiver wrote; ``missing`` when
        no retention record exists; ``unreadable`` when the record could not
        be opened or read; ``invalid`` when its content is not the bounded
        integer document this code writes.
    """

    sequence: int | None = None
    state: FloorState = "missing"


def read_sequence(path: Path) -> int | None:
    """Read the next-sequence counter without following links.

    Parameters
    ----------
    path : Path
        The counter file.

    Returns
    -------
    int | None
        The stored sequence, or None when the file does not exist.

    Raises
    ------
    ValueError
        If the file is not a regular file, is oversized, or is not a decimal
        integer.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        # A link, a permission fault or unreadable state is damage, not a
        # missing counter: report the same closed fault as a non-regular file
        # so the caller falls back to the sealed directory.
        msg = "sequence counter is not readable"
        raise ValueError(msg) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            msg = "sequence counter is not a regular file"
            raise ValueError(msg)
        raw = os.read(descriptor, _MAX_SEQUENCE_TEXT + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_SEQUENCE_TEXT:
        msg = "sequence counter is oversized"
        raise ValueError(msg)
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        msg = "sequence counter is not ASCII"
        raise ValueError(msg) from error
    if not text.isdecimal():
        msg = "sequence counter is not a decimal integer"
        raise ValueError(msg)
    return int(text)


def read_retention_floor(layout: SegmentLayout) -> RetentionFloor:
    """Read the retention floor recorded by the last successful run.

    A missing record is not the same fact as a damaged one, so the two are
    reported separately rather than both collapsing into "no floor": a fresh
    store has never recorded one, while an unreadable or invalid record is
    state the archiver wrote and can no longer trust.

    Parameters
    ----------
    layout : SegmentLayout
        The layout whose ``retention.json`` is read.

    Returns
    -------
    RetentionFloor
        The lowest retained sequence and how it was read, or the state that
        kept it from being read.
    """
    try:
        raw = read_private_bytes(layout.retention_file, limit=_MAX_STATE_BYTES)
    except FileNotFoundError:
        return RetentionFloor(state="missing")
    except OversizedFileError:
        # The file is readable but larger than this code ever writes, so its
        # content is refused as invalid rather than reported as an I/O fault.
        return RetentionFloor(state="invalid")
    except OSError:
        return RetentionFloor(state="unreadable")
    return _parse_retention_floor(raw)


def _parse_retention_floor(raw: bytes) -> RetentionFloor:
    """Decode the bounded retention record this code writes.

    Parameters
    ----------
    raw : bytes
        The record's content, already read within the size bound.

    Returns
    -------
    RetentionFloor
        The recorded sequence, or an ``invalid`` state when the content is not
        the integer document this code writes.
    """
    try:
        document = json.loads(raw)
    except ValueError, TypeError, RecursionError:
        return RetentionFloor(state="invalid")
    if not isinstance(document, dict):
        return RetentionFloor(state="invalid")
    floor = document.get("floor_sequence")
    if not isinstance(floor, int) or isinstance(floor, bool):
        return RetentionFloor(state="invalid")
    if floor < 0:
        return RetentionFloor(state="invalid")
    return RetentionFloor(sequence=floor, state="recorded")
