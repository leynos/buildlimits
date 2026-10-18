"""Ensure ordinary test collection cannot probe the live host user manager."""

import subprocess
import typing as typ
from unittest import mock

from systemd_harness import user_manager_available

if typ.TYPE_CHECKING:
    import pytest


def test_host_manager_probe_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return unavailable before probing executables or spawning a manager client."""
    monkeypatch.delenv("BUILD_LIMITS_HOST_ACCEPTANCE", raising=False)
    spawn = mock.Mock(side_effect=AssertionError("must not contact the host manager"))
    monkeypatch.setattr(subprocess, "run", spawn)
    assert not user_manager_available(), "host readiness requires explicit opt-in"
    spawn.assert_not_called()
