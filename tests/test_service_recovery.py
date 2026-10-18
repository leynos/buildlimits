"""Real notifying units refuse unknown cache work and recover after an owned stop."""

import subprocess
import time

import pytest
from harness_units import unit_properties
from systemd_harness import Harness, systemd_user_marks

from buildlimits.systemd import UserManager

pytestmark = systemd_user_marks("needs a systemd user manager")


def test_live_cache_refusal_and_verified_stop_recover_without_restarting_old_work() -> (
    None
):
    """Startup exit 2 leaves a live cache untouched until the operator accounts for it."""
    harness = Harness(1, cached=True)
    try:
        harness.start()
        units = harness.cached_units
        assert units is not None, "the fixture owns both ordered services"
        manager = UserManager()
        original = manager.show(harness.sccache_unit, "MainPID")["MainPID"]
        assert int(original) > 0, "readiness allowed the isolated cache to start"
        units._run("stop", harness.unit)
        with pytest.raises(subprocess.CalledProcessError):
            units.start()
        properties = _service_state(harness.unit)
        assert properties["ExecMainStatus"] == "2", (
            "unknown old work requires operator action"
        )
        assert properties["ActiveState"] == "failed", "admission never became ready"
        time.sleep(2.5)
        assert _service_state(harness.unit) == properties, (
            "sticky exit 2 prevents an automatic retry loop"
        )
        assert properties["NRestarts"] == "0", (
            "no unsafe accounting restart was attempted"
        )
        assert manager.show(harness.sccache_unit, "MainPID")["MainPID"] == original, (
            "startup refusal neither restarts nor terminates previous cache work"
        )
        units._run("stop", harness.sccache_unit)
        units._run("reset-failed", harness.unit)
        units.start()
        status = harness.status()
        assert status["healthy"] and status["held"] == 0, status
        assert not status["queued"], (
            "verified stopped startup owns no previous requests"
        )
    finally:
        harness.close()


def _service_state(unit: str) -> dict[str, str]:
    """Read the isolated service's exit and restart state via the public user bus.

    Returns
    -------
    dict[str, str]
        Exit status, active state and restart count from one manager query.
    """
    return unit_properties(unit, "ExecMainStatus", "ActiveState", "NRestarts")
