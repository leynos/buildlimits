"""A watched listening socket under listener-pressure control, for tests.

Examples
--------
>>> with watched_listener(lambda: 0.0) as (selector, listener, pressure):
...     selector.get_key(listener).data, pressure.paused
('listener', False)
"""

import contextlib
import selectors
import socket
import typing as typ

from buildlimits.listener_pressure import ListenerPressure

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.metrics import MetricsPort


@contextlib.contextmanager
def watched_listener(
    clock: cabc.Callable[[], float], metrics: MetricsPort | None = None
) -> cabc.Generator[tuple[selectors.BaseSelector, socket.socket, ListenerPressure]]:
    """Register a listener with a real selector and put it under pressure control.

    Parameters
    ----------
    clock : Callable[[], float]
        The monotonic clock the pressure policy reads.
    metrics : MetricsPort or None
        The emitter for pressure transitions, or None for the default.

    Yields
    ------
    tuple[selectors.BaseSelector, socket.socket, ListenerPressure]
        The selector, the listener registered with data ``"listener"``, and
        the policy that owns pausing and resuming it.
    """
    with selectors.DefaultSelector() as selector, socket.socket() as listener:
        selector.register(listener, selectors.EVENT_READ, "listener")
        yield selector, listener, ListenerPressure(selector, clock, metrics=metrics)
