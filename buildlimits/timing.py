"""Injectable clock and jitter effects for bounded waits and retries."""

import dataclasses as dc
import random
import time
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass(frozen=True, slots=True)
class Clock:
    """A monotonic clock and a sleep, replaceable in tests.

    Attributes
    ----------
    now : Callable[[], float]
        Returns monotonic seconds.
    sleep : Callable[[float], None]
        Waits for the given number of seconds.
    """

    now: cabc.Callable[[], float] = time.monotonic
    sleep: cabc.Callable[[float], None] = time.sleep


SYSTEM_CLOCK = Clock()


def seconds_since(start: float | None, now: float) -> float:
    """Measure an interval that may not have begun.

    Parameters
    ----------
    start : float | None
        Monotonic time at which the interval began, or ``None`` before it has.
    now : float
        Current monotonic time.

    Returns
    -------
    float
        Seconds since ``start``, or zero when there is no start.

    Examples
    --------
    >>> seconds_since(None, 5.0), seconds_since(2.0, 5.0)
    (0.0, 3.0)
    """
    return 0.0 if start is None else now - start


@dc.dataclass(frozen=True, slots=True)
class RetryWait:
    """Time and jitter effects for bounded descriptor-pressure retries.

    Attributes
    ----------
    clock : Clock
        Shared monotonic time and sleep used by admission deadlines.
    jitter : Callable[[float, float], float]
        Selects a multiplier within the supplied backoff bounds.
    """

    clock: Clock = SYSTEM_CLOCK
    jitter: cabc.Callable[[float, float], float] = random.SystemRandom().uniform


SYSTEM_RETRY_WAIT = RetryWait()


def as_clock(clock: cabc.Callable[[], float] | Clock) -> Clock:
    """Retain an injected clock's sleep port or pair a callable with real sleep.

    Parameters
    ----------
    clock : Callable[[], float] | Clock
        A monotonic callable or the existing time-and-sleep port.

    Returns
    -------
    Clock
        The original port, or a port using the callable and default sleep.
    """
    return clock if isinstance(clock, Clock) else Clock(now=clock)
