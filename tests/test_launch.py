"""Tests for forking, holding and releasing launch children."""

import os
import select
import signal
from pathlib import Path

import pytest
from launch_helpers import plan, wait_reaped

from buildlimits import launch


def _env() -> dict[str, str]:
    """Provide a minimal environment so a shell can find its builtins."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def _spec(argv: tuple[str, ...], cwd: Path) -> launch.LaunchSpec:
    """Build a launch spec with no descriptors to relocate."""
    return launch.LaunchSpec(
        argv=argv,
        env=_env(),
        cwd=str(cwd),
        received_fds=(),
        fd_targets=(),
        plan=plan(umask=0o22),
    )


def _open_fds() -> set[int]:
    """List this process's open descriptor numbers."""
    return {int(entry.name) for entry in Path("/proc/self/fd").iterdir()}


def _wait_readable(fd: int | None, timeout: float = 1.0) -> None:
    """Block until a descriptor is readable, or fail the test."""
    assert fd is not None, "the child still has its error pipe"
    ready, _, _ = select.select([fd], [], [], timeout)
    assert ready, f"descriptor {fd} did not become readable within {timeout}s"


def test_spawn_held_blocks_the_child_until_release(tmp_path: Path) -> None:
    """A held child must not run its compiler before release."""
    marker = tmp_path / "marker"
    spec = _spec(("/bin/sh", "-c", f"touch {marker}"), tmp_path)
    child = launch.spawn_held(spec)
    try:
        assert not marker.exists(), "the compiler has not run before release"
        child.release()
        wait_reaped(child)
        assert marker.exists(), "the compiler ran once released"
    finally:
        child.close()


@pytest.mark.parametrize(
    ("program", "expected_code"), [("/bin/true", 0), ("/bin/false", 1)]
)
def test_release_then_reap_returns_the_compilers_exit(
    tmp_path: Path, program: str, expected_code: int
) -> None:
    """Releasing a held child lets it run; reaping yields its real exit code."""
    spec = _spec((program,), tmp_path)
    child = launch.spawn_held(spec)
    try:
        child.release()
        status = wait_reaped(child)
        code, signal_number = launch.exit_of(status)
        assert code == expected_code, f"the compiler exits with code {expected_code}"
        assert signal_number is None, "a normal exit carries no signal"
    finally:
        child.close()


def test_exec_failure_is_reported(tmp_path: Path) -> None:
    """A nonexistent program fails exec and the error pipe reports it."""
    missing = tmp_path / "does-not-exist"
    spec = _spec((str(missing),), tmp_path)
    child = launch.spawn_held(spec)
    try:
        child.release()
        _wait_readable(child.errors)
        status = wait_reaped(child)
        message = launch.read_exec_outcome(child)
        assert message is not None, "a failed exec reports a message"
        assert missing.name in message, "the message names the missing program"
        assert "stage=exec" in message, "the failing stage is explicit"
        code, signal_number = launch.exit_of(status)
        assert code == launch.EXEC_FAILURE_STATUS, (
            "the child exits with the exec-failure status"
        )
        assert signal_number is None, "the child was not signalled"
    finally:
        child.close()


def test_read_exec_outcome_is_none_after_successful_exec(tmp_path: Path) -> None:
    """A compiler that starts closes the error pipe without writing to it."""
    spec = _spec(("/bin/true",), tmp_path)
    child = launch.spawn_held(spec)
    try:
        child.release()
        _wait_readable(child.errors)
        assert launch.read_exec_outcome(child) is None, (
            "a started compiler reports no failure"
        )
        wait_reaped(child)
    finally:
        child.close()


def test_read_exec_outcome_returns_none_without_an_error_pipe() -> None:
    """Reading with no error pipe reports that the compiler started."""
    child = launch.HeldChild(pid=0, barrier=None, errors=None)
    assert launch.read_exec_outcome(child) is None, "no error pipe means no failure"


def test_read_exec_outcome_reassembles_chunked_writes(tmp_path: Path) -> None:
    """A message longer than one read is returned whole and in order."""
    # A regular file holds the whole payload without depending on pipe capacity.
    payload = b"x" * 5000 + b"y" * 5000  # more than one 4096-byte read
    source = tmp_path / "errors"
    source.write_bytes(payload)
    original = os.open(source, os.O_RDONLY | os.O_CLOEXEC)
    owned = os.dup(original)
    child = launch.HeldChild(pid=0, barrier=None, errors=owned)
    try:
        os.lseek(owned, 0, os.SEEK_SET)
        message = launch.read_exec_outcome(child)
        assert message == payload.decode("utf-8"), "the reads are joined in order"
        assert child.errors is None, "the error descriptor is forgotten"
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(owned)
    finally:
        if child.errors is not None:
            os.close(child.errors)
        os.close(original)


def test_abandon_kills_and_reaps_without_running_the_compiler(tmp_path: Path) -> None:
    """An abandoned child never executes its compiler, and leaves nothing open."""
    marker = tmp_path / "marker"
    spec = _spec(("/bin/sh", "-c", f"touch {marker}"), tmp_path)
    child = launch.spawn_held(spec)
    barrier_fd, errors_fd = child.barrier, child.errors
    open_before = _open_fds()
    assert {barrier_fd, errors_fd} <= open_before, "the held child's pipes are open"
    child.abandon()
    open_after = _open_fds()
    assert child.reaped, "the abandoned child is reaped"
    assert child.barrier is None, "the barrier descriptor is forgotten"
    assert child.errors is None, "the error descriptor is forgotten"
    assert {barrier_fd, errors_fd}.isdisjoint(open_after), (
        "the held child's pipes are closed"
    )
    assert not marker.exists(), "an abandoned child never runs its compiler"


def test_close_closes_remaining_descriptors(tmp_path: Path) -> None:
    """Close releases the supervisor's descriptors once the child has exited."""
    spec = _spec(("/bin/true",), tmp_path)
    child = launch.spawn_held(spec)
    try:
        child.release()
        wait_reaped(child, timeout=10)
        errors_fd = child.errors
        assert errors_fd is not None, "the error pipe is still open before close"
        assert errors_fd in _open_fds(), "the error descriptor is open"
        child.close()
        assert errors_fd not in _open_fds(), "close closes the error descriptor"
    finally:
        if not child.reaped:
            child.abandon()
        child.close()
    assert child.errors is None, "close forgets the descriptor"


# Linux encodes a normal exit as ``code << 8`` and a fatal signal as its number.
@pytest.mark.parametrize("code", [0, 1, 127])
def test_exit_of_decodes_normal_exit_codes(code: int) -> None:
    """A normally exited child decodes to its exit code and no signal."""
    assert launch.exit_of(code << 8) == (code, None), (
        "a normal exit decodes to its code with no signal"
    )


def test_exit_of_decodes_death_by_signal() -> None:
    """A child killed by a signal decodes to that signal with no exit code."""
    assert launch.exit_of(signal.SIGTERM) == (None, signal.SIGTERM), (
        "a signalled child decodes to the signal with no exit code"
    )
