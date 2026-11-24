"""The wall and monotonic clock pair the writer reads together.

Retention compares wall-clock ages while skew detection compares how far the
wall clock moved against monotonic time. Taking both from one small value keeps
the writer's state module free of clock construction and lets a test inject
either clock without patching the system's.
"""

import time
import typing as typ

from buildlimits.utc_text import utc_now

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

__all__ = ["Clocks"]


class Clocks:
    """The wall and monotonic clocks the writer reads as a pair.

    Parameters
    ----------
    wall : Callable[[], datetime] or None
        UTC clock, defaulting to the system wall clock.
    monotonic : Callable[[], float] or None
        Monotonic clock, defaulting to ``time.monotonic``.

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
    ) -> None:
        """Bind the clocks, falling back to the system's own."""
        self.wall = utc_now if wall is None else wall
        self.monotonic = time.monotonic if monotonic is None else monotonic
