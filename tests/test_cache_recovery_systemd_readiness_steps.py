"""Readiness-specific pytest-bdd steps for cache recovery acceptance."""

import subprocess
import typing as typ

from harness_config import write_config
from pytest_bdd import given, then, when
from systemd_harness import Harness, wait_for


class RecoveryWorld(typ.Protocol):
    """The acceptance state shared by the cache recovery scenarios."""

    harness: Harness
    processes: list[subprocess.Popen[bytes]]
    queued: subprocess.Popen[bytes] | None


@given("a supervisor with 2 slots using starved recovery and sccache")
def starved_supervisor(recovery_world: RecoveryWorld) -> None:
    """Start the isolated cache with automatic starvation recovery."""
    harness = recovery_world.harness
    harness.cache_recovery = "starved"
    # Harness construction writes the default manual policy before this step.
    write_config(harness)
    harness.start()


def _assert_manual_readiness_is_blocked(world: RecoveryWorld) -> None:
    """Require the healthy manual-policy host to report blocked admission."""
    code, document = world.harness.readiness_status()
    admission = document["admission"]
    assert code == 3, "operator recovery uses the not-ready exit"
    assert document["healthy"] is True, "the supervisor itself remains healthy"
    assert admission["state"] == "blocked", "full manual-policy retention is blocked"
    assert admission["ready"] is False, "full retained capacity blocks admission"
    assert admission["server_drain"] == document["capacity"], (
        "every slot remains held for server accounting"
    )


@then("manual recovery is required for admission readiness")
@then("manual recovery remains required after a build queues")
def manual_policy_blocked(recovery_world: RecoveryWorld) -> None:
    """Distinguish healthy manual-policy status from usable admission."""
    _assert_manual_readiness_is_blocked(recovery_world)


@then("automatic recovery waits for demand and admission remains ready")
def automatic_waits_for_demand(recovery_world: RecoveryWorld) -> None:
    """Expose the starved policy's pre-demand progress path as ready."""
    code, document = recovery_world.harness.readiness_status()
    admission = document["admission"]
    recovery = document["cache_recovery"]
    assert document["healthy"] is True, "the supervisor remains healthy"
    assert recovery["automatic"] is True, "the selected policy can recover"
    assert admission["state"] == "draining", "the automatic policy waits for demand"
    assert admission["ready"] is True, "the first requester opens the recovery path"
    assert code == 0, "the current automatic recovery outlook is ready"


@when("a later build arrives to trigger automatic recovery")
def automatic_demand(recovery_world: RecoveryWorld) -> None:
    """Queue a build whose demand opens the starved-policy transaction."""
    queued = recovery_world.harness.compile("true")
    recovery_world.processes.append(queued)
    recovery_world.queued = queued
    assert wait_for(
        lambda: (
            recovery_world.harness.status()["cache_recovery"]["phase"] != "idle"
            or not recovery_world.harness.drain_fault()
        ),
        timeout=30,
    ), recovery_world.harness.journal()


@then("automatic recovery clears the drain fault and restores readiness")
def automatic_recovered(recovery_world: RecoveryWorld) -> None:
    """Wait for automatic accounting and require a ready status exit."""
    harness = recovery_world.harness
    assert wait_for(
        lambda: (
            not recovery_world.harness.drain_fault()
            and harness.status()["cache_recovery"]["phase"] == "idle"
        ),
        timeout=120,
    ), harness.journal()
    code, document = harness.readiness_status()
    admission = document["admission"]
    assert code == 0, "verified recovery restores the readiness exit"
    assert admission["state"] in {"ready", "saturated"}, (
        "normal admission follows the verified accounting"
    )
    assert admission["ready"] is True, "the status reports recovered admission"


@then("readiness remains true during ordinary saturation")
def readiness_remains_saturated(recovery_world: RecoveryWorld) -> None:
    """Keep full running capacity distinct from retained-drain blocking."""
    document = recovery_world.harness.status()
    admission = document["admission"]
    assert admission["state"] == "saturated", "a running compiler fills capacity"
    assert admission["ready"] is True, "normal saturation remains ready"
    code, _readiness_document = recovery_world.harness.readiness_status()
    assert code == 0, "normal saturation keeps the readiness exit"


@then("admission readiness is restored")
def admission_readiness_restored(recovery_world: RecoveryWorld) -> None:
    """Require ready status after the verified operator recovery."""
    code, document = recovery_world.harness.readiness_status()
    admission = document["admission"]
    assert code == 0, "verified accounting restores the readiness exit"
    assert admission["state"] in {"ready", "saturated"}, (
        "the recovered admission has a normal state"
    )
    assert admission["ready"] is True, "the recovered host is ready"
