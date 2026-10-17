"""Launch-channel and parent-owned child cleanup regressions.

Injected faults exercise held-child ownership without modifying other builds.
Run with ``uv run pytest -v
tests/test_launch_lifecycle.py`` from the repository root.
"""

import contextlib
import errno
import os
import signal
import sys
import typing as typ

import pytest
from descriptor_helpers import open_fds
from launch_helpers import clean_isolated_launch, plan, wait_reaped

from buildlimits import launch

if typ.TYPE_CHECKING:
    from buildlimits.relocation import RelocationPlan


def test_would_block_keeps_the_parent_error_reader() -> None:
    """A partial diagnostic is retained until EOF establishes the exec outcome."""
    read, write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    child = launch.HeldChild(0, None, read)
    try:
        os.write(write, b"first")
        assert launch.read_exec_outcome(child) is None, (
            "partial data is not a completed outcome"
        )
        assert child.errors == read, (
            "the reader stays registered while the writer lives"
        )
        os.write(write, b"second")
        os.close(write)
        write = -1
        assert launch.read_exec_outcome(child) == "firstsecond", (
            "all chunks survive until EOF"
        )
    finally:
        child.close()
        if write >= 0:
            os.close(write)


def test_post_fork_parent_setup_failure_abandons_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure to configure the parent's error reader must leave no held child."""
    before = open_fds()
    observed = []
    original = os.fork

    def fork() -> int:
        """Record only the parent-side child identifier."""
        pid = original()
        if pid:
            observed.append(pid)
        return pid

    def fail(*_args: object) -> typ.NoReturn:
        """Inject a parent-only setup failure after fork."""
        raise OSError(errno.EIO, "cannot configure error pipe")

    monkeypatch.setattr(os, "fork", fork)
    monkeypatch.setattr(os, "set_blocking", fail)
    spec = launch.LaunchSpec(("/bin/true",), {}, ".", (), (), plan(umask=0o22))
    try:
        with pytest.raises(OSError, match="cannot configure"):
            launch.spawn_held(spec)
        assert len(observed) == 1, "the experiment owns exactly one child"
        reaped = False
        try:
            os.waitpid(observed[0], os.WNOHANG)
        except ChildProcessError:
            reaped = True
        assert reaped, "parent setup failure must reap its own held child"
        assert open_fds() == before, (
            "post-fork failure restores the parent descriptor baseline"
        )
    finally:
        for pid in observed:
            with contextlib.suppress(ChildProcessError):
                ended, _status = os.waitpid(pid, os.WNOHANG)
                if ended == 0:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
        for fd in open_fds() - before:
            os.close(fd)


def _signal_channel_reuse(report: int) -> object:
    """Deliver a signal while placement reuses an inherited wake-up number."""
    del report
    wake_read, wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    result_read, result_write = os.pipe()
    error_read, error_write = os.pipe()
    barrier_read, barrier_write = os.pipe()
    spec = launch.LaunchSpec(
        (sys.executable, "-c", f'import os;os.write({wake_write},b"token")'),
        {},
        ".",
        (result_write,),
        (wake_write,),
        plan(umask=0o22),
    )
    pid = os.fork()
    if pid == 0:  # pragma: no cover - disposable grandchild
        try:
            signal.signal(signal.SIGUSR1, lambda *_args: None)
            signal.set_wakeup_fd(wake_write, warn_on_full_buffer=False)
            patch = pytest.MonkeyPatch()
            original_plan = launch.descriptors.execute_plan

            def after_plan(plan: RelocationPlan, kept: list[int]) -> None:
                """Signal after placement and before later signal reset."""
                original_plan(plan, kept)
                os.kill(os.getpid(), signal.SIGUSR1)

            patch.setattr(launch.descriptors, "execute_plan", after_plan)
            launch._become_compiler(spec, barrier_read, error_write)
        finally:
            os._exit(1)
    for fd in (wake_read, wake_write, result_write, error_write, barrier_read):
        os.close(fd)
    child = launch.HeldChild(pid, barrier_write, error_read)
    os.set_blocking(error_read, False)
    try:
        child.release()
        status = wait_reaped(child, timeout=10)
        message = launch.read_exec_outcome(child)
        result = os.read(result_read, 4096)
        return {
            "code": os.waitstatus_to_exitcode(status),
            "message": message,
            "data": list(result),
        }
    finally:
        if not child.reaped:
            child.abandon()
        child.close()
        os.close(result_read)


def test_inherited_signal_channel_cannot_write_to_a_relocated_pipe() -> None:
    """Recycled supervisor wake-up numbers must not corrupt compiler tokens."""
    result = clean_isolated_launch(_signal_channel_reuse)
    assert result["data"] == list(b"token"), result


def test_abandon_never_signals_a_previously_reaped_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale reaped flag cannot authorize signalling a reused process number."""
    child = launch.HeldChild(12345, None, None)
    signals = []

    def already_reaped(_pid: int, _options: int) -> typ.NoReturn:
        """Model interruption between waitpid completion and the state update."""
        raise ChildProcessError

    monkeypatch.setattr(os, "waitpid", already_reaped)
    monkeypatch.setattr(os, "kill", lambda pid, number: signals.append((pid, number)))
    child.abandon()
    assert child.reaped, "ownership has already ended"
    assert not signals, "an unowned process must never be signalled"
