"""Real systemd admission survives inherited descriptor pressure and cancellation."""

import collections.abc as cabc
import contextlib
import json
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from harness_processes import manager_nofile_limit
from harness_units import unit_properties
from pytest_bdd import given, scenarios, then, when
from systemd_harness import (
    Harness,
    kill_group,
    systemd_user_marks,
    wait_for,
)

from buildlimits.protocol import StatusRequest, encode_request, recv_frame, send_frame

pytestmark = systemd_user_marks("needs a systemd user manager")
scenarios("features/descriptor_pressure_on_systemd.feature")

_COMPILER = """import os, time
from pathlib import Path
os.fstat(255)
Path(os.environ["PRESSURE_MARKER"]).write_text(str(os.getpid()))
while not Path(os.environ["PRESSURE_RELEASE"]).exists():
    time.sleep(0.01)
"""
_CALLER = """import os, resource, sys
from buildlimits.adapter import run
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (max(256, soft), hard))
source = os.open("/dev/null", os.O_RDWR)
os.dup2(source, 255, inheritable=True)
for _ in range(18):
    os.set_inheritable(os.open("/dev/null", os.O_RDWR), True)
resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
sys.exit(run([sys.executable, "-c", os.environ["PRESSURE_COMPILER"]], cacheable=False))
"""


class PressureWorld:
    """Own one isolated supervisor and its pressure requesters."""

    def __init__(self) -> None:
        """Create only fixture ownership; startup happens in the scenario."""
        self.hard = manager_nofile_limit()
        assert self.hard >= 256, "the manager must support target descriptor 255"
        self.harness = Harness(1, nofile=(128, self.hard))
        self.process_handles = contextlib.ExitStack()
        self.processes: list[subprocess.Popen[bytes]] = []
        self.cancelled: set[int] = set()
        self.baseline: set[int] = set()
        self.pid = 0

    def descriptors(self) -> set[int]:
        """Sample the isolated supervisor's owned handles.

        Returns
        -------
        set[int]
            Current descriptor numbers, independent of the reporting socket.
        """
        return {int(path.name) for path in Path(f"/proc/{self.pid}/fd").iterdir()}

    def close(self) -> None:
        """Stop only fixture units before reaping its own requester processes."""
        self.harness.close()
        for process in self.processes:
            if process.poll() is None:
                kill_group(process)
            process.communicate(timeout=30)
        self.process_handles.close()


@pytest.fixture
def pressure_world() -> cabc.Iterator[PressureWorld]:
    """Keep all systemd units and subprocesses owned by this scenario."""
    world = PressureWorld()
    try:
        yield world
    finally:
        world.close()


@given("one compiler slot with soft limit 128 and a reachable hard limit")
def _started(pressure_world: PressureWorld) -> None:
    """Open one fixture pool under deliberately restricted supervisor headroom."""
    harness = pressure_world.harness
    harness.start()
    properties = unit_properties(
        harness.unit, "MainPID", "LimitNOFILE", "LimitNOFILESoft"
    )
    pressure_world.pid = int(properties["MainPID"])
    assert properties["LimitNOFILE"] == str(pressure_world.hard), (
        "systemd applied the hard limit"
    )
    assert properties["LimitNOFILESoft"] == "128", "systemd applied the soft limit"
    assert wait_for(lambda: harness.status()["held"] == 0), harness.journal()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(harness.runtime / "supervisor.sock"))
        send_frame(connection, encode_request(StatusRequest()))
        assert recv_frame(connection) is not None, "the reporting boundary is available"
        assert connection.recv(1) == b"", "the supervisor closed its status descriptor"
    pressure_world.baseline = pressure_world.descriptors()
    limits = harness.status()["descriptors"]
    assert limits["soft"] == 128 and limits["hard"] == pressure_world.hard, (
        "the real supervisor has the deliberately restricted effective limits"
    )


@when("six dense requests with target descriptor 255 queue concurrently")
def _queued(pressure_world: PressureWorld) -> None:
    """Fill received-right ownership while preserving each caller's high target."""
    harness = pressure_world.harness
    for index in range(6):
        environment = {
            **harness.env,
            "PRESSURE_COMPILER": _COMPILER,
            "PRESSURE_MARKER": str(harness.work / f"compiler-{index}"),
            "PRESSURE_RELEASE": str(harness.work / "release"),
        }
        directory = harness.work / f"request-{index}"
        directory.mkdir()
        pressure_world.processes.append(
            pressure_world.process_handles.enter_context(
                # The world-owned ExitStack keeps callers alive across BDD steps;
                # PressureWorld.close drains units before closing this stack.
                subprocess.Popen(  # pylint: disable=consider-using-with
                    [sys.executable, "-c", _CALLER],
                    env=environment,
                    cwd=directory,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            )
        )
    assert wait_for(lambda: len(harness.status()["queued"]) >= 2), harness.journal()
    status = harness.status()
    assert status["held"] == 1 and status["healthy"], status
    assert status["descriptors"]["high_water"] <= status["descriptors"]["budget"], (
        "queued ownership remains bounded under concurrent inherited rights"
    )


@when("one queued requester and the running requester disconnect")
def _cancelled(pressure_world: PressureWorld) -> None:
    """Cancel one of each state without touching any unrelated build."""
    harness = pressure_world.harness
    markers = [harness.work / f"compiler-{index}" for index in range(6)]
    assert wait_for(lambda: any(marker.exists() for marker in markers)), (
        harness.journal()
    )
    running = next(index for index, marker in enumerate(markers) if marker.exists())
    queued = int(
        Path(harness.status()["queued"][0]["directory"]).name.removeprefix("request-")
    )
    assert queued != running, (
        "cancellation must cover one queued and one running request"
    )
    for index in (queued, running):
        kill_group(pressure_world.processes[index], signal.SIGTERM)
        pressure_world.processes[index].wait(timeout=30)
        pressure_world.cancelled.add(index)


@when("the remaining compilers finish")
def _finished(pressure_world: PressureWorld) -> None:
    """Release the fixture's compiler stubs and await every surviving request."""
    pressure_world.harness.work.joinpath("release").touch()
    for index, process in enumerate(pressure_world.processes):
        if index not in pressure_world.cancelled:
            process.wait(timeout=60)


@then("every surviving requester succeeds with its target descriptor preserved")
def _successful(pressure_world: PressureWorld) -> None:
    """Require successful high-target execution for all uncancelled callers."""
    for index, process in enumerate(pressure_world.processes):
        if index not in pressure_world.cancelled:
            assert process.returncode == 0, process.communicate(timeout=5)
            assert (pressure_world.harness.work / f"compiler-{index}").exists(), (
                "the actual compiler validated its inherited high descriptor"
            )


@then("compiler tokens and supervisor descriptors return to baseline")
def _recovered(pressure_world: PressureWorld) -> None:
    """Wait for complete accounting rather than accepting a later health probe."""
    harness = pressure_world.harness
    assert wait_for(lambda: harness.status()["held"] == 0), harness.journal()
    assert wait_for(lambda: pressure_world.descriptors() == pressure_world.baseline), (
        "supervisor descriptors return to their exact pre-pressure numbers"
    )
    status = harness.status()
    assert status["healthy"] and not status["queued"], status
    assert status["descriptors"]["retained"] == 0, "no request retains rights"
    assert status["descriptors"]["high_water"] <= status["descriptors"]["budget"], (
        status
    )

    print(
        "issue255 pressure evidence: "
        + json.dumps(
            {
                "limits": [
                    status["descriptors"]["soft"],
                    status["descriptors"]["hard"],
                ],
                "baseline_count": len(pressure_world.baseline),
                "final_count": len(pressure_world.descriptors()),
                "high_water": status["descriptors"]["high_water"],
                "budget": status["descriptors"]["budget"],
                "held": status["held"],
                "retained": status["descriptors"]["retained"],
                "cancelled": len(pressure_world.cancelled),
                "survivors": len(pressure_world.processes)
                - len(pressure_world.cancelled),
            },
            sort_keys=True,
        )
    )
