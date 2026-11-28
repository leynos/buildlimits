"""Deciding whether one maintenance firing's clock reading can be trusted.

Age retention compares each row's observation time with the wall clock, so a
wall clock that has jumped can expire history nobody meant to discard. The
writer detects this within one process by comparing wall and monotonic time.
The archiver runs once per timer firing and keeps no process, so it records a
small baseline beside its state instead: the boot identifier, the wall time and
``CLOCK_BOOTTIME``. The next locked firing compares its own reading with that
baseline; a difference of more than one day between how far the two clocks
moved means the wall clock moved on its own.

A missing, damaged or different-boot baseline applies no guard: a fresh store
and a host that just rebooted have nothing to compare, and withholding age
eviction there would be arbitrary. The reading is still recorded, so the next
firing has a baseline.

The guard withholds age eviction for the firing that detected the jump. The
byte and record bounds still apply, because they count history rather than
dating it. The firing records its own reading whichever way it decides, so an
undisturbed next firing resumes age eviction.
"""

import dataclasses as dc
import json
import math
import typing as typ

from buildlimits.outcome_archive_fs import atomic_write_json, ensure_private_directory
from buildlimits.outcome_store_files import read_private_bytes
from buildlimits.outcome_store_io import fsync_directory
from buildlimits.utc_format import optional_utc_moment, utc_text

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_segments import SegmentLayout

SKEW_SECONDS = 24 * 60 * 60
"""How far wall and boot time may disagree before a reading is suspect.

The writer uses the same one-day threshold in-process, so a host does not
acquire a second definition of "the clock jumped" merely because the decision
was made by the timer rather than the recorder.
"""

BASELINE_SCHEMA = 1
_MAX_BASELINE_BYTES = 4096

__all__ = [
    "BASELINE_SCHEMA",
    "SKEW_SECONDS",
    "ClockReading",
    "is_skewed",
    "observe",
    "read_baseline",
    "write_baseline",
]


@dc.dataclass(frozen=True, slots=True)
class ClockReading:
    """One firing's wall time with the boot identity it belongs to.

    Attributes
    ----------
    boot_id : str | None
        The boot identifier, or None when the platform publishes none.
    wall : datetime
        The UTC wall time the firing started at.
    boottime : float | None
        Seconds since boot including suspend, or None when unavailable.
    """

    boot_id: str | None
    wall: dt.datetime
    boottime: float | None


def observe(
    *,
    wall: dt.datetime,
    boot_id: str | None,
    boottime: float | None,
) -> ClockReading:
    """Build one firing's reading from the clocks the caller supplied.

    Returns
    -------
    ClockReading
        The reading the baseline comparison uses.
    """
    return ClockReading(boot_id=boot_id, wall=wall, boottime=boottime)


def is_skewed(previous: ClockReading | None, current: ClockReading) -> bool:
    """Whether the wall clock moved independently of the boot clock.

    A comparison needs both readings' boot identity and boot time. Anything
    missing means the pair cannot be compared, and no guard applies.

    Parameters
    ----------
    previous : ClockReading | None
        The baseline the last locked firing recorded, or None when none could
        be read.
    current : ClockReading
        This firing's reading.

    Returns
    -------
    bool
        True when wall and boot time disagree by more than a day.

    Examples
    --------
    >>> import datetime as dt
    >>> base = ClockReading("boot", dt.datetime(2026, 1, 1, tzinfo=dt.UTC), 100.0)
    >>> jumped = ClockReading("boot", dt.datetime(2026, 1, 3, tzinfo=dt.UTC), 200.0)
    >>> is_skewed(base, jumped)
    True
    """
    if previous is None:
        return False
    if previous.boot_id is None or current.boot_id is None:
        return False
    if previous.boot_id != current.boot_id:
        return False
    if previous.boottime is None or current.boottime is None:
        return False
    wall_elapsed = (current.wall - previous.wall).total_seconds()
    boot_elapsed = current.boottime - previous.boottime
    return abs(wall_elapsed - boot_elapsed) > SKEW_SECONDS


def read_baseline(layout: SegmentLayout) -> ClockReading | None:
    """Read the last locked firing's reading, or None when it is unusable.

    A baseline this code did not write, or wrote and cannot parse, is treated
    as absent: the guard withholds age eviction only when two trustworthy
    readings disagree, and a damaged record is not one.

    Returns
    -------
    ClockReading | None
        The recorded reading, or None.
    """
    try:
        raw = read_private_bytes(layout.clock_baseline_file, limit=_MAX_BASELINE_BYTES)
    except OSError:
        # An absent, oversized or unreadable baseline is not evidence of a
        # jump; only two readings that can both be trusted are.
        return None
    return _parse_baseline(raw)


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
    # A firing can finish before any other state was installed, for example a
    # store that never settled on its first reading. The baseline still has to
    # be recorded, so its directory is created here rather than assumed.
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
