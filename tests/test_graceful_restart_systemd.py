"""Real-systemd acceptance: ``systemctl --user restart`` cancels nothing.

An operator installs new supervisor code and runs one command. These scenarios
run that command against an isolated supervisor, its fixture cache and a fixture
token pool, under the load that broke it on a real host: a build that is
running, a build that is waiting, a cache left live, and a token that only a
stopped cache can release. In each, every build must finish with its own exit
status, the restart must complete without operator action, and the pool must be
whole afterwards.
"""

import os
import subprocess
from pathlib import Path

import pytest
from harness_units import manager_env, unit_properties
from restart_world import (
    RestartWorld,
    assert_whole_again,
    finish,
    full_journal,
    main_pid,
    started_marker,
    stopping,
)
from systemd_harness import kill_group, systemd_user_marks, wait_for

pytestmark = [
    *systemd_user_marks("needs a systemd user manager and busctl"),
    pytest.mark.timeout(180),
]


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
