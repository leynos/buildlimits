"""Read the two small state files the segmented store keeps.

Both files are advisory: a missing or damaged counter or retention record is
reported as unknown rather than trusted, so a scan or a run still works after a
crash halfway through publishing one of them.
"""

import json
import os
import stat
import typing as typ

from buildlimits.outcome_store_io import read_private_bytes

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_segments import SegmentLayout

_MAX_SEQUENCE_TEXT = 32
# The retention record is a handful of JSON fields; anything larger is not
# state this code wrote and must not be read into memory.
_MAX_STATE_BYTES = 4096

__all__ = ["read_retention_floor", "read_sequence"]


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


def read_retention_floor(layout: SegmentLayout) -> int | None:
    """Read the retention floor recorded by the last successful run.

    Parameters
    ----------
    layout : SegmentLayout
        The layout whose ``retention.json`` is read.

    Returns
    -------
    int | None
        The lowest retained sequence, or None when no valid state exists.
    """
    try:
        raw = read_private_bytes(layout.retention_file, limit=_MAX_STATE_BYTES)
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError, TypeError, RecursionError:
        return None
    if not isinstance(document, dict):
        return None
    floor = document.get("floor_sequence")
    if not isinstance(floor, int) or isinstance(floor, bool):
        return None
    if floor < 0:
        return None
    return floor
