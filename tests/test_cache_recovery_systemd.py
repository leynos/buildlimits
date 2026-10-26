"""Opt-in systemd acceptance for retained sccache work and recovery."""

import collections.abc as cabc
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import test_cache_recovery_systemd_readiness_steps as readiness_steps
from harness_units import manager_env
from pytest_bdd import given, scenarios, then, when
from systemd_harness import (
    Harness,
    kill_group,
    systemd_user_marks,
    wait_for,
)

# The busy-host scenario waits out one stop retry (30 s of backoff) before the
# cache can stop, so the default 30 s bound is too short for it.
pytestmark = [
    *systemd_user_marks("needs a systemd user manager and busctl"),
    pytest.mark.timeout(180),
]

# pytest-bdd scopes step fixtures to the module where their decorators run.
# Register the helper functions here because this module owns the scenarios.
starved_supervisor = given(
    "a supervisor with 2 slots using starved recovery and sccache"
)(readiness_steps.starved_supervisor)
manual_policy_blocked = then("manual recovery is required for admission readiness")(
    readiness_steps.manual_policy_blocked
)
manual_policy_blocked = then("manual recovery remains required after a build queues")(
    manual_policy_blocked
)
automatic_waits_for_demand = then(
    "automatic recovery waits for demand and admission remains ready"
)(readiness_steps.automatic_waits_for_demand)
automatic_demand = when("a later build arrives to trigger automatic recovery")(
    readiness_steps.automatic_demand
)
automatic_recovered = then(
    "automatic recovery clears the drain fault and restores readiness"
)(readiness_steps.automatic_recovered)
readiness_remains_saturated = then("readiness remains true during ordinary saturation")(
    readiness_steps.readiness_remains_saturated
)
admission_readiness_restored = then("admission readiness is restored")(
    readiness_steps.admission_readiness_restored
)

scenarios("features/cache_recovery_on_systemd.feature")


class RecoveryWorld:
    """One isolated cached supervisor and the requesters it admits.

    Every build gets its own jobserver FIFO, held open read-write by this
    world. The fixture cache blocks acquiring that FIFO before it spawns its
    compiler, so an empty FIFO *is* the pre-spawn wait and the fixture's own
    ``.waiting`` marker proves the wait is armed. Most scenarios use the
    ``manual`` recovery policy; one selects ``starved`` before start to check
    automatic progress once demand arrives.
    """

    def __init__(self) -> None:
        """Create only fixture ownership; units start in the scenario."""
        self.harness = Harness(2, cached=True, cache_recovery="manual")
        self.fifos: list[Path] = []
        self.holders: list[int] = []
        self.processes: list[subprocess.Popen[bytes]] = []
        self.cancelled: list[subprocess.Popen[bytes]] = []
        self.queued: subprocess.Popen[bytes] | None = None
        self.recovered: subprocess.CompletedProcess[str] | None = None

    def start_waiting_build(self) -> subprocess.Popen[bytes]:
        """Start a cached build that stops in the pre-spawn jobserver wait.

        The FIFO is created and held read-write here, so a blocking acquire
        can only be the intended wait. The fixture server writes the
        ``.waiting`` marker after it accepts the request and immediately
        before that blocking read, so a written marker means this build has
        been admitted and no compiler has spawned for it.

        Returns
        -------
        subprocess.Popen[bytes]
            The admitted adapter, still waiting on the cache's jobserver.
        """
        jobserver = self.harness.work / f"jobserver-{len(self.fifos)}"
        os.mkfifo(jobserver)
        self.holders.append(os.open(jobserver, os.O_RDWR))
        self.fifos.append(jobserver)
        process = self.harness.compile(
            "true", env={"FAKE_SCCACHE_JOBSERVER": str(jobserver)}
        )
        self.processes.append(process)
        marker = Path(f"{jobserver}.waiting")
        assert wait_for(marker.exists), self.harness.journal()
        return process

    def cache_active(self) -> bool:
        """Report whether the fixture cache's unit is active.

        Returns
        -------
        bool
            Whether the manager currently reports the isolated cache active.
        """
        return (
            subprocess.run(
                [
                    "/usr/bin/systemctl",
                    "--user",
                    "is-active",
                    "--quiet",
                    self.harness.sccache_unit,
                ],
                check=False,
                env=manager_env(),
                timeout=30,
            ).returncode
            == 0
        )

    def stop_cache(self) -> None:
        """Stop the fixture cache's unit, as the transaction's own stop does."""
        self.harness.stop_sccache()
        assert wait_for(lambda: not self.cache_active(), timeout=30), (
            self.harness.journal()
        )

    def start_cache(self) -> None:
        """Ask the manager to start the fixture cache's unit."""
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "start", self.harness.sccache_unit],
            check=False,
            env=manager_env(),
            timeout=30,
        )

    def close(self) -> None:
        """Stop owned units, then reap processes and release held FIFOs."""
        self.harness.close()
        for process in self.processes:
            if process.poll() is None:
                kill_group(process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=30)
        for descriptor in self.holders:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        self.holders = []


@pytest.fixture
def recovery_world() -> cabc.Iterator[RecoveryWorld]:
    """Provide a scenario's world and release everything it owned."""
    world = RecoveryWorld()
    try:
        yield world
    finally:
        world.close()


@given("a supervisor with 2 slots composing builds with sccache")
def _supervisor(recovery_world: RecoveryWorld) -> None:
    """Start the isolated supervisor with its ready managed cache."""
    recovery_world.harness.start()


@given("a fixture jobserver the managed cache waits on before it spawns a compiler")
def _jobserver(recovery_world: RecoveryWorld) -> None:
    """Record the fixture contract the scenario relies on before any build runs.

    Each build brings its own jobserver device, created by the world that
    starts it, so this step proves the two preconditions those devices need:
    the cache is composed with compilations, and the pool is untouched, so
    every later deficit can only come from admission.
    """
    harness = recovery_world.harness
    assert harness.status()["caching"], "the managed cache is ready to compose"
    assert harness.free_tokens() == harness.slots, (
        "no build has been admitted yet, so the pool is at capacity"
    )


@when("two cached builds wait for a compiler slot before any compiler spawns")
def _two_waiting(recovery_world: RecoveryWorld) -> None:
    """Admit both slots' worth of builds, each blocked before it spawns."""
    recovery_world.start_waiting_build()
    recovery_world.start_waiting_build()
    harness = recovery_world.harness
    assert wait_for(lambda: harness.status()["held"] == 2), harness.journal()
    assert harness.free_tokens() == 0, "both tokens are lent to waited builds"


@when("both builds are killed while they wait")
def _both_killed(recovery_world: RecoveryWorld) -> None:
    """Kill each waited build's whole session, as an interruption would."""
    for process in tuple(recovery_world.processes):
        assert process.poll() is None, "a waited build is alive before the kill"
        recovery_world.cancelled.append(process)
        kill_group(process)
        assert process.wait(timeout=30) == -9, "the adapter was killed, not asked"


@then("both killed builds' tokens stay held with no compiler running")
def _held_without_visible_work(recovery_world: RecoveryWorld) -> None:
    """Hold every token on unaccounted work, with no compiler root to point at."""
    harness = recovery_world.harness
    assert wait_for(lambda: harness.status()["held"] == 2), harness.journal()
    assert wait_for(lambda: "retained detached work" in harness.drain_fault()), (
        harness.journal()
    )
    status = harness.status()
    assert status["drain_fault_kind"] == "live_work", status
    assert harness.server_children() == 0, (
        "no visible compiler root remains, so no token may be released for being idle"
    )
    assert harness.free_tokens() == 0, "the pool has lent both of its tokens"


@then("a later build waits for a token that is never free")
def _queued(recovery_world: RecoveryWorld) -> None:
    """Queue an unrelated requester behind the two retained tokens."""
    harness = recovery_world.harness
    queued = harness.compile("true")
    recovery_world.processes.append(queued)
    recovery_world.queued = queued
    assert wait_for(lambda: bool(harness.status()["queued"]), timeout=20), (
        harness.journal()
    )
    assert queued.poll() is None, "the waiting build must still be waiting"


@when("the operator recovers the shared admission and waits for it")
def _recover(recovery_world: RecoveryWorld) -> None:
    """Run the operator's own command against this isolated supervisor."""
    recovery_world.recovered = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from buildlimits.recover_cli import main; "
                "sys.exit(main(['--wait', '--wait-timeout', '240']))"
            ),
        ],
        env=recovery_world.harness.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


@when("one of the two builds is killed while it waits")
def _one_killed(recovery_world: RecoveryWorld) -> None:
    """Kill only the first build, leaving the second running beside it."""
    first = recovery_world.processes[0]
    recovery_world.cancelled.append(first)
    kill_group(first)
    assert first.wait(timeout=30) == -9, "the adapter was killed, not asked"
    assert recovery_world.processes[1].poll() is None, "the other build lives on"


@then("the killed build's token stays held while the other build keeps running")
def _one_retained(recovery_world: RecoveryWorld) -> None:
    """Wait for the retained-work fault with a cached build still admitted."""
    harness = recovery_world.harness
    assert wait_for(lambda: "retained detached work" in harness.drain_fault()), (
        harness.journal()
    )
    status = harness.status()
    assert status["held"] == 2, "the live build keeps its own token"


@when("the operator asks for recovery without waiting")
def _recover_nowait(recovery_world: RecoveryWorld) -> None:
    """Open the transaction beside the running cached build."""
    recovery_world.recovered = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from buildlimits.recover_cli import main; sys.exit(main([]))",
        ],
        env=recovery_world.harness.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@then("recovery opens but leaves the cache running under the live build")
def _opened_beside_live_build(recovery_world: RecoveryWorld) -> None:
    """Require an open transaction, a written barrier and an untouched cache."""
    result = recovery_world.recovered
    assert result is not None, "the operator command must have run"
    assert "cannot start" not in result.stderr + result.stdout, (
        result.stdout,
        result.stderr,
    )
    harness = recovery_world.harness
    marker = harness.runtime / "cache-recovery"
    assert wait_for(marker.exists, timeout=30), harness.journal()
    assert wait_for(
        lambda: "still moving" in str(harness.status()["cache_recovery"]["last_fault"]),
        timeout=60,
    ), harness.journal()
    assert recovery_world.cache_active(), "the stop waits for the running build"


@when("the surviving build is killed")
def _survivor_killed(recovery_world: RecoveryWorld) -> None:
    """Kill the second build so no cached work remains to wait for."""
    second = recovery_world.processes[1]
    recovery_world.cancelled.append(second)
    kill_group(second)
    assert second.wait(timeout=30) == -9, "the adapter was killed, not asked"


@then("the recovery completes and frees both held tokens")
def _recovered(recovery_world: RecoveryWorld) -> None:
    """Require the operator's own transaction to end and clear the fault.

    The waiting build may already have taken one of the released tokens, so
    the exact pool balance is only deterministic once it has finished; that
    check belongs to the step that settles it.
    """
    result = recovery_world.recovered
    assert result is not None, "the operator command must have run"
    assert result.returncode == 0, (result.stdout, result.stderr)
    harness = recovery_world.harness
    assert not harness.drain_fault(), "the retained-work fault is cleared"
    assert harness.status()["held"] <= 1, harness.journal()


@then("the waiting build succeeds")
def _queued_succeeds(recovery_world: RecoveryWorld) -> None:
    """Check that the request waiting throughout recovery made its own progress."""
    queued = recovery_world.queued
    assert queued is not None, "a request was queued behind the retained tokens"
    assert queued.wait(timeout=120) == 0, queued.communicate(timeout=5)


@then("the cache was restarted after its accounting")
def _cache_restarted(recovery_world: RecoveryWorld) -> None:
    """Check the transaction restarted the cache and conserved every token."""
    harness = recovery_world.harness
    assert wait_for(lambda: harness.status()["caching"], timeout=60), harness.journal()
    assert wait_for(recovery_world.cache_active, timeout=30), (
        "the cache unit the transaction restarted is active"
    )
    assert wait_for(
        lambda: harness.status()["held"] == 0 and harness.free_tokens() == harness.slots
    ), harness.journal()


@then("the cache refuses to start again while the restart barrier is present")
def _barrier_blocks_start(recovery_world: RecoveryWorld) -> None:
    """Check the barrier the role template installs really does stop a start."""
    marker = recovery_world.harness.runtime / "cache-recovery"
    marker.write_text("recovering\n", encoding="ascii")
    try:
        recovery_world.stop_cache()
        recovery_world.start_cache()
        assert not recovery_world.cache_active(), (
            "a start under the barrier must be skipped, not honoured"
        )
    finally:
        marker.unlink(missing_ok=True)
        recovery_world.start_cache()
    assert wait_for(recovery_world.cache_active, timeout=30), (
        "with the barrier gone the cache starts normally again"
    )
    print(
        "issue285 recovery evidence: "
        + json.dumps(
            {
                "cancelled": len(recovery_world.cancelled),
                "release_exit": (
                    recovery_world.recovered.returncode
                    if recovery_world.recovered
                    else None
                ),
                "held_after": recovery_world.harness.status()["held"],
                "pool_after": recovery_world.harness.free_tokens(),
                "capacity": recovery_world.harness.status()["capacity"],
            },
            sort_keys=True,
        )
    )
