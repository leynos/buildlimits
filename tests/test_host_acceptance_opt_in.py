"""Ensure ordinary test collection cannot probe the live host user manager."""

import os
import subprocess
from unittest import mock

import pytest
import systemd_harness
from systemd_harness import user_manager_available


def test_host_manager_probe_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return unavailable before probing executables or spawning a manager client."""
    monkeypatch.delenv("BUILD_LIMITS_HOST_ACCEPTANCE", raising=False)
    spawn = mock.Mock(side_effect=AssertionError("must not contact the host manager"))
    monkeypatch.setattr(subprocess, "run", spawn)
    assert not user_manager_available(), "host readiness requires explicit opt-in"
    spawn.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("systemctl is unavailable"),
        subprocess.TimeoutExpired("systemctl", timeout=30),
    ],
    ids=["systemctl-missing", "manager-timeout"],
)
def test_host_manager_probe_errors_skip_acceptance(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """Treat a missing or unresponsive manager as unavailable for collection."""
    monkeypatch.setitem(os.environ, "BUILD_LIMITS_HOST_ACCEPTANCE", "1")
    monkeypatch.setattr(systemd_harness.shutil, "which", lambda _name: "available")
    probe = mock.Mock(side_effect=failure)
    monkeypatch.setattr(subprocess, "run", probe)

    assert not user_manager_available(), "unavailable systemd skips host tests"
    probe.assert_called_once()
