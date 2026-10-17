"""Scripted seams for driving the compiler adapter without a supervisor.

The adapter reaches its supervisor through ``adapter.connect`` and captures
the caller's descriptors through ``adapter.descriptors``; these helpers
replace both, and supply a retry policy that never sleeps, so a test steers
a whole invocation from in-process socket pairs.

Examples
--------
>>> immediate_retry().jitter(0.0, 2.0)
2.0
"""

import typing as typ

from buildlimits import adapter
from buildlimits.timing import Clock, RetryWait

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import socket

    import pytest


def immediate_retry() -> RetryWait:
    """Retry at once, always choosing the longest jittered delay.

    Returns
    -------
    RetryWait
        A policy whose waits return immediately, so busy retries never sleep.
    """
    return RetryWait(
        clock=Clock(sleep=lambda _seconds: None), jitter=lambda _low, high: high
    )


def script_adapter(
    monkeypatch: pytest.MonkeyPatch,
    connections: cabc.Iterator[socket.socket],
    capture: cabc.Callable[..., list[int]],
) -> None:
    """Route the adapter's connections and descriptor capture to test doubles.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to replace the adapter's seams for one test.
    connections : Iterator[socket.socket]
        The client ends handed out, one per connection attempt.
    capture : Callable[..., list[int]]
        Stands in for ``inheritable_descriptors``.
    """
    monkeypatch.setattr(adapter, "connect", lambda *_args: next(connections))
    monkeypatch.setattr(adapter.descriptors, "inheritable_descriptors", capture)
