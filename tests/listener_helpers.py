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
    from pathlib import Path

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


@contextlib.contextmanager
def unix_listener(path: Path) -> cabc.Generator[socket.socket]:
    """Bind a real Unix listener and remove its socket path after use.

    Parameters
    ----------
    path : Path
        The filesystem path for the listener socket.

    Yields
    ------
    socket.socket
        The listening socket, open until the context exits.

    Examples
    --------
    ``with unix_listener(path) as listener`` yields a listening Unix socket
    whose path is removed when the context closes.
    """
    listener = open_unix_listener(path)
    try:
        yield listener
    finally:
        listener.close()
        path.unlink(missing_ok=True)


def open_unix_listener(path: Path) -> socket.socket:
    """Bind a Unix listener whose socket and path the caller manages.

    Parameters
    ----------
    path : Path
        The filesystem path for the listener socket.

    Returns
    -------
    socket.socket
        The listening socket; callers close it and remove its path.

    Raises
    ------
    OSError
        If binding the path or beginning to listen fails.

    Examples
    --------
    ``listener = open_unix_listener(path)`` leaves both socket and path open
    for a test that controls when the simulated server stops.
    """
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
    except OSError:
        listener.close()
        raise
    try:
        listener.listen(8)
    except OSError:
        listener.close()
        path.unlink(missing_ok=True)
        raise
    return listener
