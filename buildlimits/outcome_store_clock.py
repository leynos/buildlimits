"""The wall and monotonic clock pair the writer and archiver read together.

Retention compares wall-clock ages while skew detection compares how far the
wall clock moved against a monotonic reading. Taking both from one small value
keeps the writer's state module free of clock construction and lets a test
inject either clock without patching the system's.

The boot identity and ``CLOCK_BOOTTIME`` reading live here too. The archiver
compares them across invocations, where no in-process monotonic baseline
survives, and it must use a clock that keeps counting while the host is
suspended — otherwise a laptop that slept for two days would look like a
two-day wall-clock jump.
"""

import time
import typing as typ
from pathlib import Path

from buildlimits.utc_text import utc_now

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

__all__ = ["BOOT_ID_PATH", "Clocks", "system_boot_id", "system_boottime"]

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
"""Where Linux names the current boot.

The identifier changes on every reboot, so a baseline recorded before a reboot
cannot be compared with the boot time that follows: both clocks restarted.
"""


def system_boot_id() -> str | None:
    """Read the current boot's identifier.

    Returns
    -------
    str | None
        The identifier, or None when the platform does not publish one.
    """
    try:
        return BOOT_ID_PATH.read_text(encoding="ascii").strip() or None
    except OSError, UnicodeError:
        return None


def system_boottime() -> float | None:
    """Read ``CLOCK_BOOTTIME``, which keeps counting across suspend.

    Returns
    -------
    float | None
        Seconds since boot including suspended time, or None when the platform
        has no such clock.
    """
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is None:
        return None
    try:
        return time.clock_gettime(clock)
    except OSError:
        return None


class Clocks:
    """The clocks the writer and archiver read as a set.

    Parameters
    ----------
    wall : Callable[[], datetime] or None
        UTC clock, defaulting to the system wall clock.
    monotonic : Callable[[], float] or None
        Monotonic clock, defaulting to ``time.monotonic``.
    boot_id : Callable[[], str | None] or None
        Boot identifier, defaulting to the kernel's.
    boottime : Callable[[], float | None] or None
        ``CLOCK_BOOTTIME`` reading, defaulting to the system's.

    Examples
    --------
    >>> pair = Clocks()
    >>> pair.wall().tzinfo is not None
    True
    """

    def __init__(
        self,
        wall: cabc.Callable[[], dt.datetime] | None = None,
        monotonic: cabc.Callable[[], float] | None = None,
        boot_id: cabc.Callable[[], str | None] | None = None,
        boottime: cabc.Callable[[], float | None] | None = None,
    ) -> None:
        """Bind the clocks, falling back to the system's own."""
        self.wall = utc_now if wall is None else wall
        self.monotonic = time.monotonic if monotonic is None else monotonic
        self.boot_id = system_boot_id if boot_id is None else boot_id
        self.boottime = system_boottime if boottime is None else boottime
