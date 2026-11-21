"""The archive command as a timer runs it: a real process with a real status.

The in-process command tests exercise parsing and rendering, but a systemd
timer starts a separate process and records only its exit status. These tests
launch the installed console script, and the module form as its documented
fallback, then observe the status and the store the next firing would find.
Two running processes are also raced for the archive lock so the busy status
is proved across processes rather than within one.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from archive_cli_helpers import (
    REPO_ROOT,
    SCRIPT,
    command_arguments,
    sealed_source,
    write_config,
)
from outcome_archive_helpers import NOW, policy, records, write
from segment_test_helpers import document

from buildlimits.outcome_archive_lock import ArchiveLock
from buildlimits.outcome_segments import SegmentLayout


def _invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the installed command with ``arguments`` and capture its output.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The finished process, its status and its captured output.
    """
    return subprocess.run(
        [SCRIPT, *arguments],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _lock_is_held(layout: SegmentLayout, pid: int) -> bool:
    """Report whether ``/proc/locks`` shows ``pid`` holding the archive lock.

    Parameters
    ----------
    layout : SegmentLayout
        The layout naming the lock file.
    pid : int
        The process whose lock entry is sought.

    Returns
    -------
    bool
        True when a ``FLOCK`` entry for the lock file's inode names ``pid``.
    """
    try:
        metadata = layout.lock_file.stat()
        listing = Path("/proc/locks").read_text(encoding="ascii")
    except OSError:
        return False
    device = f"{os.major(metadata.st_dev):02x}:{os.minor(metadata.st_dev):02x}"
    expected = f"{device}:{metadata.st_ino}"
    return any(
        parts[1] == "FLOCK" and parts[4] == str(pid) and parts[5] == expected
        for line in listing.splitlines()
        if len(parts := line.split()) >= 6
    )


def _await_lock(layout: SegmentLayout, pid: int, *, timeout: float = 60.0) -> None:
    """Wait until the first run has taken the archive lock.

    The lock, not an archive, is the barrier: it is the state the second run
    must meet, and it is observable before any work is published.

    Parameters
    ----------
    layout : SegmentLayout
        The layout naming the lock file.
    pid : int
        The first run's process identifier.
    timeout : float
        How long to wait before failing the test.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _lock_is_held(layout, pid):
            return
        time.sleep(0.005)
    pytest.fail("the first run never took the archive lock before the deadline")


def test_the_installed_command_inspects_dry_runs_and_archives(
    tmp_path: Path,
) -> None:
    """The timer's entry point reports, plans and then publishes in turn."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 3), policy(1))
    config = write_config(tmp_path / "supervisor.toml")
    layout = SegmentLayout.beside(store)
    arguments = command_arguments(store, config)

    inspected = _invoke("inspect", *arguments, "--json")

    assert inspected.returncode == 0, inspected.stderr
    inspected_report = json.loads(inspected.stdout)
    assert inspected_report["dry_run"] is True, "inspection executes nothing"
    assert inspected_report["plan"]["archive"] == 3, "each sealed source is planned"

    dry = _invoke("run", "--dry-run", *arguments)

    assert dry.returncode == 0, dry.stderr
    assert "dry run: no file was changed" in dry.stdout, "the marker is explicit"
    assert not list(layout.archive_dir.glob("*.jsonl.xz")), (
        "the dry run publishes nothing"
    )

    ran = _invoke("run", *arguments)

    assert ran.returncode == 0, ran.stderr
    assert "status: ok" in ran.stdout, "the run reports success"
    assert len(list(layout.archive_dir.glob("*.jsonl.xz"))) == 3, (
        "every sealed source is archived"
    )
    assert not list(layout.sealed_dir.glob("*.jsonl")), "the sources are released"


def test_the_module_form_runs_the_same_command(tmp_path: Path) -> None:
    """``python -m buildlimits.outcome_archive_cli`` is a supported launch."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy(1))
    config = write_config(tmp_path / "supervisor.toml")

    finished = subprocess.run(
        [
            sys.executable,
            "-m",
            "buildlimits.outcome_archive_cli",
            "inspect",
            *command_arguments(store, config),
            "--json",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert finished.returncode == 0, finished.stderr
    assert json.loads(finished.stdout)["status"] == "ok", "the same store is reported"


def test_a_corrupt_source_exits_three(tmp_path: Path) -> None:
    """A segment that cannot be decoded is an operator-actionable failure."""
    store = tmp_path / "cache-outcomes.jsonl"
    sealed_source(store, 1, b"not a header\n")
    config = write_config(tmp_path / "supervisor.toml")

    finished = _invoke("run", *command_arguments(store, config))

    assert finished.returncode == 3, finished.stderr
    assert "status: corrupt" in finished.stdout, "the status is printed"
    assert "failure: sequence 1: corrupt" in finished.stdout, "the reason is bounded"


def test_a_lock_held_by_another_process_exits_seventy_five(
    tmp_path: Path,
) -> None:
    """A concurrent run is retryable across processes, never waited on."""
    store = tmp_path / "cache-outcomes.jsonl"
    sealed_source(store, 1, document(records(NOW, 1)))
    config = write_config(tmp_path / "supervisor.toml")
    layout = SegmentLayout.beside(store)

    with ArchiveLock(layout.lock_file):
        finished = _invoke("run", *command_arguments(store, config))

    assert finished.returncode == 75, finished.stderr
    assert "status: busy" in finished.stdout, "the status is printed"
    assert "reason: busy" in finished.stdout, "the reason is closed"


def _hold_first(first: subprocess.Popen[bytes], layout: SegmentLayout) -> None:
    """Stop the first run while it holds the archive lock.

    A stopped process keeps its open descriptors, and with them the ``flock``
    the archive lock holds, so the second command meets a lock that is held for
    as long as the test needs. ``CLD_STOPPED`` alone would only prove the
    process stopped, so the lock entry is re-read afterwards: a process stopped
    after releasing the lock would let the second run acquire it, and the busy
    status would be asserted against a lock that is no longer held.
    ``WNOWAIT`` reads the stop without consuming the exit status the final wait
    needs.
    """
    first.send_signal(signal.SIGSTOP)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        state = os.waitid(
            os.P_PID, first.pid, os.WEXITED | os.WSTOPPED | os.WNOHANG | os.WNOWAIT
        )
        if state is not None:
            if state.si_code != os.CLD_STOPPED:
                pytest.fail("the first run left before it reached the barrier")
            if not _lock_is_held(layout, first.pid):
                pytest.fail("the first run stopped after releasing the archive lock")
            return
        time.sleep(0.01)
    pytest.fail("the first run did not stop at the barrier before the deadline")


def _reap(first: subprocess.Popen[bytes]) -> None:
    """End one run without an unbounded wait on any path.

    A ``Popen`` context manager waits for the process on exit with no timeout.
    This does the bounded equivalent: an already-finished run is left alone,
    and a live one is killed and waited for with a deadline.

    Raises
    ------
    RuntimeError
        If the process is still running after a kill and a bounded wait.
    """
    if first.poll() is not None:
        return
    first.kill()
    try:
        first.wait(timeout=10)
    except subprocess.TimeoutExpired:
        msg = "the first run survived SIGKILL"
        raise RuntimeError(msg) from None


def test_two_real_runs_do_not_archive_at_once(tmp_path: Path) -> None:
    """A second timer firing during a run reports busy instead of racing.

    The first run is synchronized on its own acquisition of the archive lock,
    then stopped at a barrier while the second command runs. Stopping the
    process keeps its lock, so the second run provably overlaps the first
    rather than relying on compression time. The barrier is released only
    after the second command has reported busy.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 24_000), policy(512 * 1024))
    config = write_config(tmp_path / "supervisor.toml", preset=6)
    layout = SegmentLayout.beside(store)
    arguments = command_arguments(store, config)

    # The first run's output goes to files: a pipe would fill and block the
    # run, and a file keeps the report readable after the process is reaped.
    output_path = tmp_path / "first.out"
    errors_path = tmp_path / "first.err"
    with (
        output_path.open("w", encoding="utf-8") as output,
        errors_path.open("w", encoding="utf-8") as errors,
    ):
        first = subprocess.Popen(
            [SCRIPT, "run", *arguments], cwd=REPO_ROOT, stdout=output, stderr=errors
        )
        try:
            _await_lock(layout, first.pid)
            _hold_first(first, layout)
            try:
                second = _invoke("run", *arguments)
            finally:
                first.send_signal(signal.SIGCONT)
            first.wait(timeout=180)
        finally:
            _reap(first)

    assert second.returncode == 75, second.stderr
    assert "status: busy" in second.stdout, "the overlapping run reports busy"
    assert first.returncode == 0, errors_path.read_text()
    assert "status: ok" in output_path.read_text(), "the first run completes its work"
    assert not list(layout.sealed_dir.glob("*.jsonl")), (
        "the first run archived the whole history"
    )
