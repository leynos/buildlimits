"""Exhausted listener retries cannot spin or make diagnostics fatal."""

import errno
import typing as typ

from listener_helpers import watched_listener

from buildlimits import listener_pressure
from buildlimits.descriptor_budget import DescriptorBudget

if typ.TYPE_CHECKING:
    import pytest


def test_listener_waits_for_a_real_tick_before_retrying() -> None:
    """Continuous readiness cannot turn accepted descriptor pressure into a busy loop."""
    now = [0.0]
    with watched_listener(lambda: now[0]) as (selector, listener, pressure):
        pressure.pause(listener, errno.EMFILE, DescriptorBudget(), 0)
        pressure.resume(listener)
        assert pressure.paused, "same-tick readiness cannot retry admission"
        assert not selector.get_map(), (
            "the exhausted listener stays outside the selector"
        )
        now[0] = 0.1
        pressure.resume(listener)
        assert not pressure.paused, "a later timed tick restores acceptance"
        assert selector.get_key(listener).data == "listener", (
            "backlog ownership persists"
        )


def test_listener_pressure_survives_a_failed_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pressure diagnostic cannot change admission health or watch ownership."""
    with watched_listener(lambda: 0.0) as (selector, listener, pressure):

        def fail(_message: str) -> None:
            """Refuse an optional journal write while listener ownership is intact."""
            raise BrokenPipeError(errno.EPIPE, "injected pressure diagnostic failure")

        monkeypatch.setattr(listener_pressure, "log", fail)
        pressure.pause(listener, errno.EMFILE, DescriptorBudget(), 0)
        assert pressure.paused, "pressure remains a controlled listener state"
        assert not selector.get_map(), "existing listener ownership stays paused"
