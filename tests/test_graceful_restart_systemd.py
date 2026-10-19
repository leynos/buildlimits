"""Real-systemd acceptance: ``systemctl --user restart`` cancels nothing.

An operator installs new supervisor code and runs one command. These scenarios
run that command against an isolated supervisor, its fixture cache and a fixture
token pool, under the load that broke it on a real host: a build that is
running, a build that is waiting, a cache left live, and a token that only a
stopped cache can release. In each, every build must finish with its own exit
status, the restart must complete without operator action, and the pool must be
whole afterwards.
"""

import collections.abc as cabc
import contextlib
import os
import subprocess
from pathlib import Path

import pytest
from harness_processes import reap_and_close_pipes
from harness_units import manager_env, unit_properties
from systemd_harness import Harness, journal, kill_group, systemd_user_marks, wait_for

pytestmark = [
    *systemd_user_marks("needs a systemd user manager and busctl"),
    pytest.mark.timeout(180),
]


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
        process = subprocess.Popen(  # pylint: disable=consider-using-with
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


@pytest.fixture
def world() -> cabc.Iterator[RestartWorld]:
    """Provide a scenario's world and release everything it owned."""
    state = RestartWorld()
    try:
        yield state
    finally:
        state.close()


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
    assert wait_for(harness._listening, timeout=60), harness.journal()
    assert wait_for(lambda: bool(harness.status()["caching"]), timeout=30), (
        harness.journal()
    )
    status = harness.status()
    assert status["healthy"], status
    assert status["held"] == 0, "no token is held after the restart"
    assert harness.free_tokens() == harness.slots, "the pool is whole"


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


def test_a_restart_waits_for_a_running_cached_build(world: RestartWorld) -> None:
    """The build finishes with its own status; only then does the supervisor go."""
    harness = world.supervisor(2)
    before = main_pid(harness)
    marker = started_marker(harness, "running")
    build = world.build(harness, f"echo $$ > {marker}; sleep 6; exit 7")
    assert wait_for(marker.exists), harness.journal()
    group = harness.status()["groups"][0]
    assert group["cached"], "the build runs composed with the managed cache"

    command = world.restart(harness)
    assert wait_for(lambda: stopping(harness), timeout=15), harness.journal()
    assert build.poll() is None, "the admitted compile outlives the restart request"
    assert command.poll() is None, "the restart waits rather than cancelling"

    status, _err = finish(world.builds[0])
    assert status == 7, "the build ended with its own exit status, uncancelled"
    assert command.wait(timeout=90) == 0, command.stderr
    assert main_pid(harness) not in {"0", before}, "new code is running"
    assert_whole_again(harness)
    log = full_journal(harness)
    assert "finishing running compiler groups" in log, log
    assert "unhealthy" not in log, "the restart was never a health failure"


def test_a_restart_requeues_a_waiting_build(world: RestartWorld) -> None:
    """A queued build is told to retry, reconnects, and completes."""
    harness = world.supervisor(1)
    marker = started_marker(harness, "holder")
    world.build(harness, f"echo $$ > {marker}; sleep 6")
    assert wait_for(marker.exists), harness.journal()
    waiting = world.build(harness, "echo ran-after-restart")
    assert wait_for(lambda: bool(harness.status()["queued"]), timeout=20), (
        harness.journal()
    )

    command = world.restart(harness)
    assert wait_for(lambda: stopping(harness), timeout=15), harness.journal()
    assert waiting.poll() is None, "the waiting build is not refused outright"

    holder_status, _ = finish(world.builds[0])
    assert holder_status == 0, "the running build completed"
    assert command.wait(timeout=90) == 0, command.stderr
    try:
        status, err = finish(waiting)
    except subprocess.TimeoutExpired:
        pytest.fail(full_journal(harness) + "\nSTATUS " + str(harness.status()))
    assert status == 0, f"the waiting build failed instead of retrying: {err}"
    assert "the supervisor is restarting" in err, "the wait is explained to the user"
    assert_whole_again(harness)


def test_a_restart_with_an_idle_live_cache_needs_no_operator(
    world: RestartWorld,
) -> None:
    """Incident: the new supervisor used to exit 2 on the cache the old left live."""
    harness = world.supervisor(2)
    cache = harness.sccache_unit
    before = unit_properties(cache, "MainPID")["MainPID"]
    assert before != "0", "the cache is live before the restart"

    command = world.restart(harness)
    assert command.wait(timeout=90) == 0, command.stderr
    assert_whole_again(harness)
    assert unit_properties(harness.unit, "ActiveState")["ActiveState"] == "active", (
        "the supervisor came back by itself"
    )
    after = unit_properties(cache, "MainPID")["MainPID"]
    assert after not in {"0", before}, "the live cache was stopped and restarted"
    assert not (harness.runtime / "cache-recovery").exists(), "no barrier is left"
    log = full_journal(harness)
    assert "start-up requires operator action" not in log, log


def test_a_restart_releases_a_token_only_a_stopped_cache_can(
    world: RestartWorld,
) -> None:
    """A cancelled cached build's token is released by the restart itself."""
    harness = world.supervisor(2, recovery="manual")
    jobserver = harness.work / "jobserver"
    os.mkfifo(jobserver)
    world.holders.append(os.open(jobserver, os.O_RDWR))
    build = world.build(harness, "true", env={"FAKE_SCCACHE_JOBSERVER": str(jobserver)})
    assert wait_for(Path(f"{jobserver}.waiting").exists), harness.journal()
    kill_group(build)
    build.wait(timeout=30)
    assert wait_for(lambda: harness.status()["held"] == 1, timeout=20), (
        harness.journal()
    )

    command = world.restart(harness)
    assert command.wait(timeout=150) == 0, (command.stderr, harness.journal())
    assert_whole_again(harness)
    assert "manual recovery of the managed cache" in full_journal(harness), (
        "the closing supervisor opened the transaction that freed its token"
    )


def test_sigint_remains_the_immediate_stop(world: RestartWorld) -> None:
    """The escape hatch still cancels a running build, and the unit comes back."""
    harness = world.supervisor(1)
    marker = started_marker(harness, "cancelled")
    build = world.build(harness, f"echo $$ > {marker}; sleep 60")
    assert wait_for(marker.exists), harness.journal()
    subprocess.run(
        ["/usr/bin/systemctl", "--user", "kill", "--signal=SIGINT", harness.unit],
        env=manager_env(),
        check=True,
        timeout=30,
    )
    status, _err = finish(build)
    assert status != 0, "the immediate stop cancelled the running build"
    assert wait_for(harness._listening, timeout=60), harness.journal()
