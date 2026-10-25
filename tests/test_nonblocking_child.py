"""Non-blocking helper-child deadlines, output bounds and ownership."""

import os
import subprocess
import sys
import time
from unittest import mock

from buildlimits.nonblocking_child import (
    MAX_ACTIVE_CHILDREN,
    MAX_OUTPUT_BYTES,
    ChildReaper,
    ChildResult,
    NonBlockingChild,
)


def _wait_for_result(
    reaper: ChildReaper, child: NonBlockingChild, now: float
) -> ChildResult:
    """Poll the bounded reaper until a test child reaches a terminal result.

    Example:
        result = _wait_for_result(reaper, child, deadline)
    """
    for _attempt in range(200):
        reaper.poll(now)
        if child.result is not None:
            return child.result
        time.sleep(0.01)
    raise AssertionError("the helper child did not reach a terminal result")


def _start_sleeping_child(
    reaper: ChildReaper, deadline: float
) -> NonBlockingChild | None:
    """Start a long-lived test process owned by the supplied reaper."""
    return reaper.start(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        os.environ,
        deadline=deadline,
    )


def test_deadline_kills_and_reaps_the_child_and_closes_its_pipe() -> None:
    """A due child is killed, reaped and detached from its output descriptor."""
    reaper = ChildReaper()
    child = _start_sleeping_child(reaper, 2.0)

    assert child is not None, "an available reaper slot starts the child"
    assert child.stdout_open, "the child owns one output pipe while running"
    result = _wait_for_result(reaper, child, 2.0)

    assert result.failure == "timeout", "the deadline has a bounded reason"
    assert child.reaped, "the killed process is reaped"
    assert not child.stdout_open, "the child output pipe is closed"


def test_output_is_capped_at_64_kibibytes() -> None:
    """Excess child output becomes a failure without growing the buffer."""
    reaper = ChildReaper()
    script = f"import os; os.write(1, b'x' * {MAX_OUTPUT_BYTES + 1})"
    child = reaper.start((sys.executable, "-c", script), os.environ, deadline=10.0)

    assert child is not None, "an available reaper slot starts the child"
    result = _wait_for_result(reaper, child, 0.0)

    assert result.failure == "read_error", "overflow is a bounded read failure"
    assert len(result.output) <= MAX_OUTPUT_BYTES, "retained output stays capped"
    assert not child.stdout_open, "overflow closes the pipe immediately"


def test_reaper_refuses_a_fifth_live_child() -> None:
    """The reaper never owns more child processes than its fixed bound."""
    reaper = ChildReaper()
    children = [_start_sleeping_child(reaper, 2.0) for _ in range(MAX_ACTIVE_CHILDREN)]

    assert all(child is not None for child in children), "four slots can be filled"
    assert (
        reaper.start((sys.executable, "-c", "pass"), os.environ, deadline=2.0) is None
    ), "a full reaper skips another child"

    for child in children:
        assert child is not None, "each filled reaper slot owns a child"
        _wait_for_result(reaper, child, 2.0)
    assert reaper.active_count == 0, "reaped children release their slots"


def test_cancel_kills_the_child_and_keeps_it_owned_until_reaped() -> None:
    """Shutdown cancellation closes the pipe before the next non-blocking reap."""
    reaper = ChildReaper()
    child = _start_sleeping_child(reaper, 20.0)

    assert child is not None, "an available reaper slot starts the child"
    reaper.cancel(child)

    assert not child.stdout_open, "cancellation closes the pipe immediately"
    assert reaper.active_count == 1, "the reaper retains ownership until poll reaps it"
    result = _wait_for_result(reaper, child, 0.0)

    assert result.failure == "cancelled", "shutdown has its own terminal reason"
    assert child.reaped, "the next non-blocking poll reaps the cancelled child"
    assert reaper.active_count == 0, "reaping releases the process slot"


def test_popen_uses_only_the_bounded_helper_process_contract() -> None:
    """Pin the shell-free streams and exclude process-group setup."""
    argv = (sys.executable, "-c", "pass")
    environment = {"PATH": "/usr/bin"}
    process = mock.Mock()
    process.stdout = mock.Mock()
    process.stdout.fileno.return_value = 37

    with (
        mock.patch(
            "buildlimits.nonblocking_child.subprocess.Popen", return_value=process
        ) as popen,
        mock.patch("buildlimits.nonblocking_child.os.set_blocking") as set_blocking,
    ):
        child = ChildReaper().start(argv, environment, deadline=2.0)

    assert child is not None, "the fake process occupies one reaper slot"
    popen.assert_called_once_with(
        list(argv),
        stdin=mock.ANY,
        stdout=mock.ANY,
        stderr=mock.ANY,
        env=environment,
        bufsize=0,
        close_fds=True,
    )
    keyword_arguments = popen.call_args.kwargs
    assert keyword_arguments["stdin"] == subprocess.DEVNULL, "stdin is DEVNULL"
    assert keyword_arguments["stdout"] == subprocess.PIPE, "stdout is a pipe"
    assert keyword_arguments["stderr"] == subprocess.DEVNULL, "stderr is DEVNULL"
    assert "preexec_fn" not in keyword_arguments, "no pre-exec callback is used"
    assert "start_new_session" not in keyword_arguments, "no process group is set"
    set_blocking.assert_called_once_with(37, False)
