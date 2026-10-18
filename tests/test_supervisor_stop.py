"""Tests for how a stop request reaches the supervisor's event loop."""

import typing as typ

from buildlimits.config import SupervisorConfig
from buildlimits.supervisor import Supervisor

if typ.TYPE_CHECKING:
    from buildlimits.systemd import UserManager


def test_a_stop_request_waits_for_the_event_loop() -> None:
    """A signal only records the stop; the next tick drains and exits cleanly."""
    # A clock before every periodic check keeps the tick to the stop alone.
    supervisor = Supervisor(
        SupervisorConfig(), typ.cast("UserManager", object()), clock=lambda: -1.0
    )
    with supervisor.selector:
        supervisor.request_stop()
        assert supervisor.admission.healthy, "the request itself changes no admission"
        assert supervisor.exit_status is None, "the request itself does not exit"
        assert supervisor.serve() == 0, "an idle supervisor stops cleanly"
        assert not supervisor.admission.healthy, "the loop stopped admitting"
