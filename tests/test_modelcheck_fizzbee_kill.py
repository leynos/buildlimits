"""The adapter kills a run's whole process group, not only its launcher.

The ``fizz`` launcher starts a Go binary of its own, so a launcher that never
exits is only the visible half of the problem. The child inherits the
launcher's process group and its stdout pipe, which separates the two failures
an adapter can have here. Killing only the launcher leaves a live checker on
the host the run was supposed to release; waiting only on the launcher leaves
``communicate`` blocked on a pipe a child still holds, and the run never
returns.

Each test drives the real adapter against a stub that starts such a child and
then reads the child's state from ``/proc``, because a child that became a
zombie still occupies a slot. The stubs themselves are written by the tests in
:mod:`tests.test_modelcheck_fizzbee_process`, which owns their helpers; this
module imports them rather than repeating them.
"""

import os
import threading
import time
from pathlib import Path

import pytest
from test_modelcheck_fizzbee_process import _spec, _stub

from modelcheck import domain
from modelcheck.adapters.fizzbee_process import FizzbeeChecker

_NEVER_EXITS = """\
#!/usr/bin/env bash
# Starts a child that outlives a launcher-only kill, then waits forever.
sleep 300 &
printf '%s\\n' "$!" > "$(dirname "$0")/child.pid"
printf 'Valid Nodes: 3 Unique states: 3\\n'
wait
"""
"""A launcher whose group the adapter must kill.

The child is what makes this a process-group test rather than a process test:
it inherits the launcher's stdout pipe and its process group, so an adapter
that killed only the launcher would both leave the child alive and block
forever collecting output from a pipe the child still holds.
"""

_STUBBORN_CHILD = """\
#!/usr/bin/env bash
# The child ignores SIGTERM and holds the pipe; the launcher obeys SIGTERM.
trap '' TERM
sleep 300 &
printf '%s\\n' "$!" > "$(dirname "$0")/child.pid"
printf 'Valid Nodes: 3 Unique states: 3\\n'
trap 'exit 0' TERM
wait
"""
"""A launcher that dies on ``SIGTERM`` while its child refuses to.

An ignored signal is inherited across ``exec``, so the child keeps ignoring
``SIGTERM`` after the launcher stops. This is the pair that separates a kill
which escalates from one that only waits on the launcher: waiting on the
launcher ends immediately, while the child still holds the stdout pipe the
adapter must read to completion.
"""

_EXIT_TIMEOUT_SECONDS = 5.0
"""How long a killed process is given to leave ``/proc`` before it counts as alive."""

_EXIT_POLL_SECONDS = 0.02
"""How often ``/proc`` is re-read while waiting for that exit."""


def _is_gone(pid: int, timeout_seconds: float = _EXIT_TIMEOUT_SECONDS) -> bool:
    """Wait for a signalled process to disappear, and report whether it did.

    ``SIGKILL`` is delivered asynchronously. The adapter's ``communicate``
    returns as soon as the killed child closes the pipe, which it may do a few
    milliseconds before the kernel stops listing it as running, so a single
    read can catch a process that is already dying and report it as a survivor.
    Polling turns that race into a bounded wait: a process that was really
    killed leaves ``/proc``, and one that was not is still there when the
    deadline passes.

    Parameters
    ----------
    pid : int
        The process to look for.
    timeout_seconds : float
        How long to keep watching before deciding it survived.

    Returns
    -------
    bool
        Whether ``/proc`` no longer holds a live or zombie entry for it, or its
        entry is a zombie — the two states a killed child reaches when its
        parent is gone.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        except FileNotFoundError, ProcessLookupError:
            return True
        state = stat.rpartition(")")[2].split()[0]
        if state == "Z":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_EXIT_POLL_SECONDS)


def _child_pid(tmp_path: Path) -> int:
    """Return the process id the stub's child wrote down.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The scratch directory the stub was written into.

    Returns
    -------
    int
        The child's process id.
    """
    return int((tmp_path / "child.pid").read_text(encoding="utf-8").strip())


def test_a_run_that_never_returns_is_killed_with_its_children(
    tmp_path: Path,
) -> None:
    """The whole process group dies, and no descendant survives.

    This is the process-control requirement. The stub's child inherits the
    launcher's group and its stdout pipe, so killing only the launcher would
    leave a live process and a pipe nothing would ever close. The assertion is
    on the child's exit, read from ``/proc``, because a child that became a
    zombie still occupies a slot on the host the run was supposed to release.
    """
    fizz = _stub(tmp_path, _NEVER_EXITS)
    checker = FizzbeeChecker(fizz, timeout_seconds=1.0)

    result = checker.check(_spec())

    child = _child_pid(tmp_path)
    assert isinstance(result, domain.Errored), "a run that never returns proves nothing"
    assert "did not finish within" in result.detail, "and the detail names the timeout"
    assert _is_gone(child), f"child {child} survived the timeout"


def test_a_launcher_that_exits_still_has_its_stubborn_child_killed(
    tmp_path: Path,
) -> None:
    """``SIGKILL`` follows even when the launcher itself obeyed ``SIGTERM``.

    Waiting on the launcher is not waiting on its group. A checker whose
    launcher exits cleanly while a descendant ignores the signal would pass a
    launcher-only wait and then hold the adapter's stdout pipe open forever.
    The run is executed on a worker so the regression fails the test rather
    than hanging the suite: a blocked worker is left behind as a daemon, and
    the assertions read the outcome only once it has finished.
    """
    fizz = _stub(tmp_path, _STUBBORN_CHILD)
    outcome: dict[str, object] = {}

    worker = threading.Thread(
        target=lambda: outcome.update(
            result=FizzbeeChecker(fizz, timeout_seconds=1.0).check(_spec())
        ),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=30.0)

    assert not worker.is_alive(), "the run returned rather than blocking on the pipe"
    result = outcome["result"]
    assert isinstance(result, domain.Errored), "a stopped run proves nothing"
    assert "did not finish within" in result.detail, "and the detail names the timeout"
    child = _child_pid(tmp_path)
    assert _is_gone(child), f"child {child} survived a launcher that exited first"


def test_a_timeout_names_the_limit_and_keeps_what_printed(tmp_path: Path) -> None:
    """The report says which bound was exceeded, and where the run had got.

    The state count a killed run managed is the evidence of whether it was
    still exploring or stuck, so the detail carries the checker's output after
    the reason rather than discarding it.
    """
    fizz = _stub(tmp_path, _NEVER_EXITS)

    result = FizzbeeChecker(fizz, timeout_seconds=1.0).check(_spec())

    assert isinstance(result, domain.Errored), "the timed-out run is an error"
    assert result.detail.splitlines()[0] == "the checker did not finish within 1s", (
        "the first line names the bound that was exceeded"
    )
    assert "Valid Nodes: 3" in result.detail, (
        "and what the run managed is kept after the reason"
    )


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="needs /proc to read a child's state"
)
def test_a_killed_group_releases_its_pipe(tmp_path: Path) -> None:
    """The run returns rather than blocking on a pipe a child still holds.

    A surviving child inheriting stdout would keep ``communicate`` waiting for
    an end of file that never comes, so a test that completes at all is
    evidence the group was killed rather than only the launcher.
    """
    fizz = _stub(tmp_path, _NEVER_EXITS)

    started = os.times().elapsed
    FizzbeeChecker(fizz, timeout_seconds=1.0).check(_spec())
    elapsed = os.times().elapsed - started

    assert elapsed < 60, "the run returned rather than blocking on the pipe"
    child = _child_pid(tmp_path)
    assert _is_gone(child), f"child {child} outlived the group it was killed with"
