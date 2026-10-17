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
