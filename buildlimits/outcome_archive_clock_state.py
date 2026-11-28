"""Read and write the clock baseline beside the outcome store.

The guard compares two readings; this adapter is the only part that touches
the filesystem. A baseline can be absent, damaged, or unreadable for an
environmental reason, and the caller needs to tell those apart: absence and
damage are documented no-guard states, while a read failure is a maintenance
fault the report must name.
"""

import dataclasses as dc
import json
import math
import typing as typ

from buildlimits.outcome_archive_clock_guard import (
    BASELINE_SCHEMA,
    ClockReading,
)
from buildlimits.outcome_archive_fs import atomic_write_json, ensure_private_directory
from buildlimits.outcome_store_files import read_private_bytes
from buildlimits.outcome_store_io import fsync_directory
from buildlimits.utc_format import optional_utc_moment, utc_text

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_segments import SegmentLayout

_MAX_BASELINE_BYTES = 4096

type BaselineState = typ.Literal["recorded", "missing", "damaged", "unreadable"]

__all__ = ["BaselineRead", "BaselineState", "read_baseline", "write_baseline"]


@dc.dataclass(frozen=True, slots=True)
class BaselineRead:
    """One attempt to read the baseline the last locked firing recorded.

    Attributes
    ----------
    reading : ClockReading | None
        The recorded reading, or None when none can be used.
    state : BaselineState
        ``recorded`` for a usable reading, ``missing`` when no file exists,
        ``damaged`` when the content is not the document this code writes, and
        ``unreadable`` when the file could not be read for another reason. The
        first three states apply no guard; ``unreadable`` is also reported as a
        maintenance fault.
    """

    reading: ClockReading | None
    state: BaselineState


def read_baseline(layout: SegmentLayout) -> BaselineRead:
    """Read the last locked firing's reading and say how it was read.

    Returns
    -------
    BaselineRead
        The reading, or the state that kept one from being read.
    """
    try:
        raw = read_private_bytes(layout.clock_baseline_file, limit=_MAX_BASELINE_BYTES)
    except FileNotFoundError:
        return BaselineRead(reading=None, state="missing")
    except OSError:
        return BaselineRead(reading=None, state="unreadable")
    reading = _parse_baseline(raw)
    if reading is None:
        return BaselineRead(reading=None, state="damaged")
    return BaselineRead(reading=reading, state="recorded")


def write_baseline(layout: SegmentLayout, reading: ClockReading) -> None:
    """Record one locked firing's reading durably.

    Parameters
    ----------
    layout : SegmentLayout
        The layout whose baseline file is replaced.
    reading : ClockReading
        The reading the next firing compares with.

    Notes
    -----
    A filesystem failure propagates as ``OSError`` so the caller can name it.
    """
    ensure_private_directory(layout.clock_baseline_file.parent)
    atomic_write_json(layout.clock_baseline_file, _baseline_document(reading))
    fsync_directory(layout.clock_baseline_file.parent)


def _baseline_document(reading: ClockReading) -> dict[str, object]:
    """Render one reading as the stable baseline document.

    Returns
    -------
    dict[str, object]
        A JSON-compatible mapping with no filesystem path.
    """
    return {
        "schema": BASELINE_SCHEMA,
        "boot_id": reading.boot_id,
        "wall": utc_text(reading.wall),
        "boottime": reading.boottime,
    }


def _parse_baseline(raw: bytes) -> ClockReading | None:
    """Decode one bounded baseline document.

    Returns
    -------
    ClockReading | None
        The reading, or None when the document is not the shape this code
        writes.
    """
    try:
        document = json.loads(raw)
    except ValueError, TypeError, RecursionError:
        return None
    if not isinstance(document, dict) or document.get("schema") != BASELINE_SCHEMA:
        return None
    try:
        return ClockReading(
            boot_id=_boot_id(document.get("boot_id")),
            wall=_wall(document.get("wall")),
            boottime=_boottime(document.get("boottime")),
        )
    except TypeError, ValueError:
        return None


def _boot_id(value: object) -> str | None:
    """Read the optional boot identity, refusing a value of the wrong shape.

    Returns
    -------
    str | None
        The identifier, or None when the platform's field was null.

    Raises
    ------
    TypeError
        If the value is neither text nor null.
    """
    if value is None or isinstance(value, str):
        return value
    msg = "a baseline boot identity must be text"
    raise TypeError(msg)


def _boottime(value: object) -> float | None:
    """Read the optional boot-time seconds, refusing a wrong-shaped value.

    Returns
    -------
    float | None
        The reading, or None when it was null. A wrong-shaped value is
        refused by ``_finite_float``.
    """
    if value is None:
        return None
    return _finite_float(value)


def _finite_float(value: object) -> float:
    """Convert one JSON number, refusing a value no clock could have read.

    Returns
    -------
    float
        The finite reading.

    Raises
    ------
    TypeError
        If the value is not a finite number. A JSON integer can exceed the
        float range and a JSON exponent can name infinity, so both are refused
        rather than allowed to escape as an unhandled conversion.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = "a baseline boot time must be a number"
        raise TypeError(msg)
    try:
        reading = float(value)
    except OverflowError:
        msg = "a baseline boot time is out of range"
        raise TypeError(msg) from None
    if not math.isfinite(reading):
        msg = "a baseline boot time must be finite"
        raise TypeError(msg)
    return reading


def _wall(value: object) -> dt.datetime:
    """Read the required wall time, refusing a missing or malformed value.

    Returns
    -------
    datetime
        The recorded UTC wall time.

    Raises
    ------
    ValueError
        If the value is missing or does not parse as a UTC moment. A value that
        is present but not text is refused the same way.
    """
    moment = optional_utc_moment(
        value,
        label="baseline wall time",
        message="a baseline wall time must be text",
    )
    if moment is None:
        msg = "a baseline wall time is required"
        raise ValueError(msg)
    return moment
