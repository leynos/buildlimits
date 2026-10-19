"""Shared world for the real-systemd restart scenarios.

``RestartWorld`` owns the isolated supervisors, the builds run against them and
the ``systemctl restart`` commands, and releases every one of them at teardown.
The small readers beside it say what a scenario waits for or requires.
"""

import contextlib
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed fixture argv, never a shell.
import typing as typ

import pytest
from harness_processes import reap_and_close_pipes
from harness_units import manager_env, unit_properties
from systemd_harness import Harness, journal, kill_group, wait_for

if typ.TYPE_CHECKING:
    from pathlib import Path


class RestartWorld:
    """Isolated supervisors, and the builds and restarts run against them."""

    def __init__(self) -> None:
        """Create no harness, build or restart yet."""
        self.harnesses: list[Harness] = []
        self.builds: list[subprocess.Popen[bytes]] = []
        self.restarts: list[subprocess.Popen[str]] = []
        self.holders: list[int] = []

    def supervisor(self, slots: int, *, recovery: str = "starved") -> Harness:
        """Start an isolated cached supervisor and wait until it composes.

        Returns
        -------
        Harness
            The started harness.
        """
        harness = Harness(slots, cached=True, cache_recovery=recovery)
        self.harnesses.append(harness)
        harness.start()
        return harness

    def build(
        self, harness: Harness, script: str, *, env: dict[str, str] | None = None
    ) -> subprocess.Popen[bytes]:
        """Start a build whose output is captured.

        Returns
        -------
        subprocess.Popen[bytes]
            The running adapter.
        """
        process = harness.compile(script, piped=True, env=env)
        self.builds.append(process)
        return process

    def restart(self, harness: Harness) -> subprocess.Popen[str]:
        """Run the operator's one documented command, without waiting for it.

        Returns
        -------
        subprocess.Popen[str]
            The ``systemctl restart`` process, blocking until the new
            supervisor is ready. The world closes its pipes at teardown.
        """
        # The world owns the process and closes it in ``close``, so a ``with``
        # block here would end the command the scenario still waits on.
        process = subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv; pylint: disable=consider-using-with
            ["/usr/bin/systemctl", "--user", "restart", harness.unit],
            env=manager_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.restarts.append(process)
        return process

    def close(self) -> None:
        """Reap processes, close their pipes, release FIFOs and stop units."""
        for process in self.builds:
            if process.poll() is None:
                kill_group(process)
        for process in self.restarts:
            if process.poll() is None:
                process.kill()
        for process in (*self.builds, *self.restarts):
            reap_and_close_pipes(process)
        for descriptor in self.holders:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        for harness in self.harnesses:
            harness.close()


def stopping(harness: Harness) -> bool:
    """Report whether the old supervisor is in the middle of its stop.

    Returns
    -------
    bool
        Whether systemd reports the unit ``deactivating`` while it drains.
    """
    return unit_properties(harness.unit, "ActiveState")["ActiveState"] == "deactivating"


def main_pid(harness: Harness) -> str:
    """Read the supervisor's current main process id.

    Returns
    -------
    str
        The id, ``0`` while no process runs.
    """
    return unit_properties(harness.unit, "MainPID")["MainPID"]


def started_marker(harness: Harness, name: str) -> Path:
    """Name the file a build writes once its compiler is running.

    Returns
    -------
    Path
        The marker inside the harness's work directory.
    """
    return harness.work / name


def assert_whole_again(harness: Harness) -> None:
    """Require a ready supervisor, a whole pool and a composed cache."""
    if not wait_for(harness._listening, timeout=60):
        pytest.fail(harness.journal())
    if not wait_for(lambda: bool(harness.status()["caching"]), timeout=30):
        pytest.fail(harness.journal())
    status = harness.status()
    if not status["healthy"]:
        pytest.fail(f"the supervisor is unhealthy after the restart: {status}")
    if status["held"] != 0:
        pytest.fail(f"a token is held after the restart: {status}")
    if harness.free_tokens() != harness.slots:
        pytest.fail("the pool is not whole after the restart")


def full_journal(harness: Harness) -> str:
    """Return enough of the unit's journal to cover the whole restart.

    Returns
    -------
    str
        The journal lines, oldest first.
    """
    return journal(harness.unit, lines=600)


def finish(process: subprocess.Popen[bytes]) -> tuple[int, str]:
    """Wait for a build and return its status and stderr.

    Returns
    -------
    tuple[int, str]
        The adapter's exit status and what it wrote to stderr.
    """
    _out, err = process.communicate(timeout=90)
    return process.returncode, err.decode(errors="replace")
