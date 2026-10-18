"""Real notifying units stop a live cache at start-up and refuse what they cannot prove."""

import collections.abc as cabc
import contextlib
import subprocess
import threading
import typing as typ

import pytest
from harness_units import unit_properties
from systemd_harness import Harness, systemd_user_marks, wait_for

from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    from cached_unit_files import CachedUnits

pytestmark = systemd_user_marks("needs a systemd user manager")


@contextlib.contextmanager
def _live_cache() -> cabc.Iterator[tuple[Harness, CachedUnits, str]]:
    """Run an isolated cached supervisor and yield its units and live cache id.

    Yields
    ------
    tuple[Harness, CachedUnits, str]
        The harness, the units it installed, and the cache's main process id.
    """
    harness = Harness(1, cached=True)
    try:
        harness.start()
        units = harness.cached_units
        assert units is not None, "the fixture owns both ordered services"
        original = UserManager().show(harness.sccache_unit, "MainPID")["MainPID"]
        assert int(original) > 0, "readiness allowed the isolated cache to start"
        yield harness, units, original
    finally:
        harness.close()


def _let_an_unwanted_restart_show_itself() -> None:
    """Wait longer than the unit's restart delay, so a retry loop would be visible."""
    threading.Event().wait(2.5)


def _assert_clean(harness: Harness) -> None:
    """Require a healthy supervisor that owns no token and no queued request."""
    status = harness.status()
    state = (status["healthy"], status["held"], status["queued"])
    assert state == (True, 0, []), f"a verified start-up owns nothing: {status}"


def _cache_pid(harness: Harness) -> str:
    """Read the isolated cache's main process id.

    Returns
    -------
    str
        The id, ``0`` while the cache is stopped.
    """
    return UserManager().show(harness.sccache_unit, "MainPID")["MainPID"]


def test_start_up_stops_a_live_cache_instead_of_refusing_it() -> None:
    """A cache left live by the previous supervisor is stopped, then restored."""
    with _live_cache() as (harness, units, original):
        units._run("stop", harness.unit)
        units.start()
        properties = _service_state(harness.unit)
        assert properties["ActiveState"] == "active", "start-up needed no operator"
        assert properties["NRestarts"] == "0", "and no automatic retry"
        assert wait_for(lambda: bool(harness.status()["caching"]), timeout=30), (
            harness.journal()
        )
        assert _cache_pid(harness) not in {"0", original}, (
            "the old cache was stopped and a new one started behind the barrier"
        )
        _assert_clean(harness)


def test_a_stop_that_cannot_be_made_safe_still_refuses_and_recovers() -> None:
    """Exit 2 remains for a cache start-up cannot stop under the barrier."""
    with _live_cache() as (harness, units, original):
        barrier = harness.runtime / "cache-recovery"
        # A directory where the barrier file belongs cannot be written, so the
        # stop is never queued: the stop must not be issued without the barrier.
        barrier.mkdir()
        units._run("stop", harness.unit)
        with pytest.raises(subprocess.CalledProcessError):
            units.start()
        properties = _service_state(harness.unit)
        assert properties["ExecMainStatus"] == "2", (
            "an unprovable cache requires operator action"
        )
        assert properties["ActiveState"] == "failed", "admission never became ready"
        _let_an_unwanted_restart_show_itself()
        assert _service_state(harness.unit) == properties, (
            "sticky exit 2 prevents an automatic retry loop"
        )
        assert properties["NRestarts"] == "0", "no unsafe restart was attempted"
        assert _cache_pid(harness) == original, (
            "startup refusal neither restarts nor terminates previous cache work"
        )
        barrier.rmdir()
        units._run("reset-failed", harness.unit)
        units.start()
        _assert_clean(harness)


def _service_state(unit: str) -> dict[str, str]:
    """Read the isolated service's exit and restart state via the public user bus.

    Returns
    -------
    dict[str, str]
        Exit status, active state and restart count from one manager query.
    """
    return unit_properties(unit, "ExecMainStatus", "ActiveState", "NRestarts")
