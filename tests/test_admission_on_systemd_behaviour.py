"""Acceptance scenarios against a real supervisor and systemd user manager."""

import collections.abc as cabc
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from systemd_harness import (
    Harness,
    kill_group,
    pid_alive,
    systemd_user_marks,
    wait_for,
)

pytestmark = systemd_user_marks("needs a systemd user manager and busctl")

scenarios("features/admission_on_systemd.feature")


class World:
    """State shared between the steps of one scenario."""

    def __init__(self) -> None:
        """Create the world with no harness, processes or outputs yet."""
        self.harness: Harness | None = None
        self.processes: list[subprocess.Popen[bytes]] = []
        self.outputs: list[tuple[bytes, bytes]] = []
        self.pids: dict[str, int] = {}

    @property
    def h(self) -> Harness:
        """The harness, once created."""
        assert self.harness is not None, "a scenario step must create the harness first"
        return self.harness

    def marker(self, name: str) -> Path:
        """Name a file in the work directory."""
        return self.h.work / name

    def read_pid(self, name: str) -> int:
        """Wait for and read a pid file written by a compiler."""
        path = self.marker(name)
        assert wait_for(lambda: path.exists() and bool(path.read_text().strip())), name
        return int(path.read_text())


@pytest.fixture
def world() -> cabc.Iterator[World]:
    """Provide a scenario's world and clean it up afterwards."""
    state = World()
    yield state
    for process in state.processes:
        kill_group(process)
        process.wait(timeout=30)
    if state.harness is not None:
        state.harness.close()


@given(parsers.re(r"a supervisor with (?P<slots>\d+) slots?$"))
def _supervisor(world: World, slots: str) -> None:
    """Start a supervisor with the requested number of slots."""
    world.harness = Harness(int(slots))
    world.h.start()


@given(
    parsers.re(r"a supervisor with (?P<slots>\d+) slots? composing builds with sccache")
)
def _cached_supervisor(world: World, slots: str) -> None:
    """Start a supervisor that composes builds with sccache."""
    world.harness = Harness(int(slots), cached=True)
    world.h.start()


@when(parsers.parse("{count:d} builds run concurrently"))
def _concurrent(world: World, count: int) -> None:
    """Run the given number of builds concurrently."""
    log = world.marker("events")
    script = f"echo start >> {log}; sleep 1; echo end >> {log}"
    world.processes = [world.h.compile(script) for _ in range(count)]
    for process in world.processes:
        process.wait(timeout=60)


@then("every build succeeds")
def _all_succeed(world: World) -> None:
    """Check that every build exits successfully."""
    assert [p.returncode for p in world.processes] == [0] * len(world.processes), (
        "every build exits successfully"
    )


@then(parsers.parse("no more than {limit:d} compilers ever ran at once"))
def _bounded(world: World, limit: int) -> None:
    """Check that concurrency never exceeded the given limit."""
    running = peak = 0
    for line in world.marker("events").read_text().split():
        running += 1 if line == "start" else -1
        peak = max(peak, running)
    assert peak == limit, "the peak concurrency never exceeds the configured limit"


@when("a build reads its input, writes to both streams and exits with 7")
def _streams(world: World) -> None:
    """Run a build that reads stdin, writes to both streams and exits with 7."""
    process = world.h.compile(
        'read line; echo "out:$line"; echo "err:$line" >&2; exit 7',
        piped=True,
    )
    world.processes.append(process)
    world.outputs.append(process.communicate(b"hello\n", timeout=60))


@then(parsers.parse("the adapter exits with {code:d}"))
def _exit_code(world: World, code: int) -> None:
    """Check that the adapter exits with the given code."""
    assert world.processes[-1].returncode == code, "the adapter's exit code matches"


@then("the build's streams are intact")
def _intact(world: World) -> None:
    """Check that the build's stdout and stderr are passed through unaltered."""
    out, err = world.outputs[-1]
    assert out == b"out:hello\n", "stdout is passed through unaltered"
    assert b"err:hello\n" in err, "stderr is passed through unaltered"


@when("a build's compiler is killed by SIGTERM")
def _sigterm(world: World) -> None:
    """Run a build whose compiler kills itself with SIGTERM."""
    process = world.h.compile("kill -TERM $$")
    world.processes.append(process)
    process.wait(timeout=60)


@then("the adapter is killed by SIGTERM")
def _killed_by_sigterm(world: World) -> None:
    """Check that the adapter is killed by the same signal as its compiler."""
    assert world.processes[-1].returncode == -signal.SIGTERM, (
        "the adapter is killed by the same signal as its compiler"
    )


@when("a build creates a file under umask 077")
def _umask(world: World) -> None:
    """Run a build that creates a file under umask 077."""
    previous = os.umask(0o077)
    try:
        process = world.h.compile(f"touch {world.marker('private')}")
    finally:
        os.umask(previous)
    world.processes.append(process)
    assert process.wait(timeout=60) == 0, "the build under umask 077 succeeds"


@then("the file is readable by its owner only")
def _private(world: World) -> None:
    """Check that the created file is readable by its owner only."""
    assert world.marker("private").stat().st_mode & 0o777 == 0o600, (
        "the created file's mode reflects the caller's umask"
    )


@when("a build starting a background process is cancelled while running")
def _cancel_running(world: World) -> None:
    """Run a build with a background process, then cancel it while running."""
    script = (
        f"sleep 300 & echo $! > {world.marker('background')}; "
        f"echo $$ > {world.marker('leader')}; wait"
    )
    process = world.h.compile(script)
    world.processes.append(process)
    world.pids["leader"] = world.read_pid("leader")
    world.pids["background"] = world.read_pid("background")
    kill_group(process)
    process.wait(timeout=30)


@then("the compiler and its background process are terminated")
def _group_gone(world: World) -> None:
    """Check that the compiler and its background process are terminated."""
    assert wait_for(lambda: not pid_alive(world.pids["leader"])), (
        "the compiler leader is terminated"
    )
    assert wait_for(lambda: not pid_alive(world.pids["background"])), (
        "the background process is terminated"
    )


@then("the slot is free again")
def _free(world: World) -> None:
    """Check that the slot is free again."""
    assert wait_for(lambda: world.h.status()["held"] == 0), world.h.journal()


@when("a second build is cancelled while it waits for the first")
def _cancel_waiting(world: World) -> None:
    """Start a first build, queue a second, then cancel the second."""
    first = world.h.compile(f"echo x > {world.marker('first-running')}; sleep 3")
    world.processes.append(first)
    assert wait_for(lambda: world.marker("first-running").exists()), (
        "the first build starts running"
    )
    second = world.h.compile(f"echo ran > {world.marker('second-ran')}")
    world.processes.append(second)
    assert wait_for(lambda: len(world.h.status()["queued"]) == 1), (
        "the second build queues behind the first"
    )
    kill_group(second)
    second.wait(timeout=30)


@then("the second compiler never ran")
def _never_ran(world: World) -> None:
    """Check that the second compiler never ran."""
    world.processes[0].wait(timeout=60)
    time.sleep(0.5)
    assert not world.marker("second-ran").exists(), "the second compiler never ran"


@then("the first build still succeeds")
def _first_ok(world: World) -> None:
    """Check that the first build still succeeds."""
    assert world.processes[0].returncode == 0, (
        "cancelling the second build does not affect the first"
    )


@when("a build's compiler runs the adapter again")
def _nested(world: World) -> None:
    """Run a build whose compiler runs the adapter again."""
    inner = f"{world.h.adapter} /bin/sh -c 'echo nested > {world.marker('nested')}'"
    process = world.h.compile(inner)
    world.processes.append(process)
    process.wait(timeout=30)


@then("the nested compiler runs without waiting")
def _nested_ran(world: World) -> None:
    """Check that the nested compiler ran without waiting."""
    assert world.processes[-1].returncode == 0, "the nested build succeeds"
    assert world.marker("nested").read_text() == "nested\n", (
        "the nested compiler ran without waiting"
    )


@when("a build's compiler leaves a background process and exits")
def _leftover(world: World) -> None:
    """Run a build whose compiler leaves a background process and exits."""
    script = f"setsid sleep 300 & echo $! > {world.marker('orphan')}; exit 0"
    process = world.h.compile(script)
    world.processes.append(process)
    process.wait(timeout=60)
    world.pids["orphan"] = world.read_pid("orphan")


@then("the build succeeds")
def _succeeds(world: World) -> None:
    """Check that the build succeeds."""
    assert world.processes[-1].returncode == 0, "the build exits successfully"


@then("the background process is terminated")
def _orphan_gone(world: World) -> None:
    """Check that the leftover background process is terminated."""
    assert wait_for(lambda: not pid_alive(world.pids["orphan"])), (
        "the leftover background process is terminated"
    )


@when("the supervisor crashes while a build compiles")
def _crash(world: World) -> None:
    """Kill the supervisor while a build compiles."""
    process = world.h.compile(f"echo $$ > {world.marker('survivor')}; sleep 300")
    world.processes.append(process)
    world.pids["survivor"] = world.read_pid("survivor")
    world.h.kill_supervisor()
    assert pid_alive(world.pids["survivor"]), (
        "the compiler survives the supervisor crash"
    )


@when("the supervisor starts again")
def _restart(world: World) -> None:
    """Start the supervisor again."""
    world.h.start()


@then("the surviving compiler has been terminated")
def _survivor_gone(world: World) -> None:
    """Check that the surviving compiler has been terminated."""
    assert not pid_alive(world.pids["survivor"]), (
        "the surviving compiler is terminated once the supervisor restarts"
    )


@then("a new build succeeds")
def _new_build(world: World) -> None:
    """Check that a new build succeeds after the restart."""
    process = world.h.compile("true")
    world.processes.append(process)
    assert process.wait(timeout=60) == 0, "a new build succeeds after the restart"


@when("a cached build is cancelled while the server compiles it")
def _cached_cancel(world: World) -> None:
    """Cancel a cached build while the server compiles it."""
    release = world.marker("release")
    script = f"while [ ! -e {release} ]; do sleep 0.1; done"
    process = world.h.compile(script)
    world.processes.append(process)
    assert wait_for(lambda: world.h.server_children() == 1), (
        "the server starts compiling before the build is cancelled"
    )
    kill_group(process)
    process.wait(timeout=30)
    time.sleep(1.5)


@then("the server is still compiling")
def _server_busy(world: World) -> None:
    """Check that the server is still compiling."""
    assert world.h.server_children() == 1, "the server is still compiling"


@then("the slot is still held")
def _still_held(world: World) -> None:
    """Check that the slot is still held."""
    status = world.h.status()
    assert status["held"] == 1, status


@when("the server's compilation finishes")
def _server_done(world: World) -> None:
    """Observe visible work ending without inferring detached request completion."""
    world.marker("release").touch()
    assert wait_for(lambda: world.h.server_children() == 0), (
        "visible compiler processes finish before testing the live-idle token"
    )


@when("the managed sccache server stops")
def _server_stops(world: World) -> None:
    """Stop the managed sccache server."""
    world.h.stop_sccache()
    assert wait_for(lambda: world.h.status()["caching"] is False, timeout=15), (
        "the supervisor notices that the server has gone"
    )


@when("a build runs while the server is down")
def _uncached_build(world: World) -> None:
    """Run a build while the server is down."""
    parent = world.marker("parent")
    process = world.h.compile(
        f"tr '\\0' ' ' < /proc/$PPID/cmdline > {parent}", piped=True
    )
    world.processes.append(process)
    world.outputs.append(process.communicate(b"", timeout=60))
    assert process.returncode == 0, world.outputs[-1]


@then("the build was not started through the sccache client")
def _not_through_client(world: World) -> None:
    """Check that the build was not started through the sccache client."""
    parent = world.marker("parent").read_text()
    assert parent, "the compiler recorded its parent's command line"
    assert "fake_sccache.py" not in parent, parent


@then("the build's standard error says the cache is unavailable")
def _notice(world: World) -> None:
    """Check that the build's standard error says the cache is unavailable."""
    _stdout, stderr = world.outputs[-1]
    assert b"compiling without the cache" in stderr, stderr
