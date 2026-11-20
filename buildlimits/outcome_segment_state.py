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

from buildlimits.outcome_segments import MAX_SEQUENCE
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
# The writer records one past the name it has just sealed, so the counter holds
# this value exactly when the last nameable sequence has been used. It is the
# writer's own record of exhaustion, and distinguishing it from damage keeps a
# later seal from reusing a name retention has since removed.
_EXHAUSTED_SEQUENCE = MAX_SEQUENCE + 1

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
        The stored sequence, or None when the file does not exist. The largest
        value returned is ``MAX_SEQUENCE + 1``: the writer stores one past the
        name it sealed, so that value records that no nameable sequence is
        left rather than a file the inventory cannot see.

    Raises
    ------
    ValueError
        If the file is not a regular file, is oversized, is not a decimal
        integer, or exceeds the exhausted marker the writer records.
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
    sequence = int(text)
    if sequence > _EXHAUSTED_SEQUENCE:
        # A counter beyond the exhausted marker names a file the twelve-digit
        # inventory cannot see, so every later seal would vanish from reports.
        # Report the damage and let the caller fall back to directory names.
        msg = "sequence counter is out of range"
        raise ValueError(msg)
    return sequence


def read_retention_floor(layout: SegmentLayout) -> RetentionFloor:
    """Read the retention floor recorded by the last successful run.


    ``retention.json`` is the record every run installs. When a run cannot
    replace it, that run leaves ``retention-pending.json`` instead, so both
    files are read and the higher valid floor wins. The floor is evidence
    that a sequence was removed on purpose, and the fallback record is only
    ever written for a floor the run has already computed, so preferring the
    higher value cannot excuse a hole the run did not remove.

    Parameters
    ----------
    layout : SegmentLayout
        The layout whose retention records are read.

    Returns
    -------
    RetentionFloor
        The lowest retained sequence and how it was read, or the state that
        kept it from being read.
    """
    primary = _floor_in(layout.retention_file)
    pending = _floor_in(layout.pending_retention_file)
    recorded = [floor for floor in (primary, pending) if floor.state == "recorded"]
    if recorded:
        # The fallback record is only ever written for a floor the run has
        # already computed, so the higher valid value cannot excuse a hole the
        # run did not remove.
        return max(recorded, key=lambda floor: floor.sequence or 0)
    for floor in (primary, pending):
        # A record this code wrote but cannot trust outranks an absent one:
        # damage is state the archiver has to report, not a fresh store.
        if floor.state in {"unreadable", "invalid"}:
            return RetentionFloor(state=floor.state)
    return RetentionFloor(state="missing")


def _floor_in(path: Path) -> RetentionFloor:
    """Read one retention record's floor, naming how it could not be read.

    Returns
    -------
    RetentionFloor
        The recorded floor, or the state that kept it from being read.
    """
    try:
        raw = read_private_bytes(path, limit=_MAX_STATE_BYTES)
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
