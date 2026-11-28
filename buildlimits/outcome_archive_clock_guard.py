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
import typing as typ

if typ.TYPE_CHECKING:
    import datetime as dt

SKEW_SECONDS = 24 * 60 * 60
"""How far wall and boot time may disagree before a reading is suspect.

The writer uses the same one-day threshold in-process, so a host does not
acquire a second definition of "the clock jumped" merely because the decision
was made by the timer rather than the recorder.
"""

BASELINE_SCHEMA = 1

__all__ = [
    "BASELINE_SCHEMA",
    "SKEW_SECONDS",
    "ClockReading",
    "is_skewed",
    "observe",
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
