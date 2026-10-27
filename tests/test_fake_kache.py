"""The kache stand-in reproduces the wrapper and daemon shapes acceptance needs.

ADR 004 requires an admitted group to be pointed at a daemon that is already
answering and never to leave one of its own behind. The fixture must therefore
stand in for both halves of that model: a status document that names the
endpoint the supervisor probed, so a daemon answering somewhere else is not
mistaken for the configured one, and a compiler that runs as the wrapper's own
child, so a group's cancellation is what removes it.

The leak mode is checked the same way the daemon guard will see it: a live
process whose executable is *named* kache, left outside any admitted scope.
"""

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import typing as typ
from pathlib import Path

from harness_processes import kill_and_reap, pid_alive, start_bound

if typ.TYPE_CHECKING:
    import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "fake_kache.py"

_SPEC = importlib.util.spec_from_file_location("fake_kache", FIXTURE)
assert _SPEC is not None and _SPEC.loader is not None, (
    "the fixture module must be loadable by path"
)
fake_kache = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fake_kache)


def _daemon(socket_path: Path) -> subprocess.Popen[bytes]:
    """Start the fixture daemon on a private socket.

    Parameters
    ----------
    socket_path : Path
        The socket the daemon is told to bind.

    Returns
    -------
    subprocess.Popen[bytes]
        The running daemon; the caller stops it in a ``finally`` block.
    """
    env = {**os.environ, "KACHE_SOCKET_PATH": str(socket_path)}
    # The handle deliberately outlives this helper so the caller can probe the
    # daemon and then stop it.
    return start_bound(
        [sys.executable, str(FIXTURE), "daemon", "run"],
        env,
        socket_path,
        stderr=subprocess.DEVNULL,
    )


def _status(
    socket: str | None, capsys: pytest.CaptureFixture[str]
) -> dict[str, object]:
    """Run the fixture's status probe and decode the document it printed."""
    assert fake_kache.status(socket) == 0, (
        "a stopped daemon is a document, not a probe failure"
    )
    return json.loads(capsys.readouterr().out)


def test_a_stopped_daemon_is_reported_in_the_document(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Absence is a field, so the probe can run on every admission cycle."""
    assert _status(None, capsys) == {"daemon_running": False}, (
        "an unset socket names no daemon and echoes none"
    )
    assert fake_kache.is_running(None) is False, "no socket is never running"
    assert fake_kache.is_running("/nonexistent/kache.sock") is False, (
        "an unreachable socket is not running"
    )


def test_a_running_daemon_echoes_the_socket_it_was_probed_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The socket field is what lets a supervisor trust the answer it read."""
    socket_path = tmp_path / "kache.sock"
    daemon = _daemon(socket_path)
    try:
        document = _status(str(socket_path), capsys)
        assert document == {"daemon_running": True, "socket": str(socket_path)}, (
            "the document names the daemon's endpoint"
        )
    finally:
        kill_and_reap(daemon, timeout=10)


def test_a_daemon_answering_elsewhere_does_not_vouch_for_the_configured_socket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A daemon on another endpoint must read as unavailable.

    This is the case a bare "is anything running" probe gets wrong: another
    harness's daemon, or a stale one, answers while the configured endpoint is
    dead, and every group composed against it would queue behind the wrong
    store.
    """
    serving = tmp_path / "serving.sock"
    configured = tmp_path / "configured.sock"
    daemon = _daemon(serving)
    try:
        document = _status(str(configured), capsys)
        assert document == {"daemon_running": False, "socket": str(configured)}, (
            "the probe reports its own endpoint, not the one that answered"
        )
    finally:
        kill_and_reap(daemon, timeout=10)


def test_the_compiler_is_the_wrappers_own_child(tmp_path: Path) -> None:
    """A cancelled group removes the compiler because kache is its parent.

    The wrapper must not ``exec`` the compiler: a replaced wrapper would leave
    the compiler as the group leader with nothing left to relay its status.
    """
    reported = tmp_path / "parent"
    script = (
        "import os, pathlib, sys; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getppid()), encoding='ascii')"
    )
    with subprocess.Popen(
        [sys.executable, str(FIXTURE), sys.executable, "-c", script, str(reported)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, f"the compiler succeeded: {stderr}"
        assert not stdout and not stderr, "the wrapper invents no output"
        assert int(reported.read_text(encoding="ascii")) == process.pid, (
            "the compiler's parent is the wrapper, so the compiler stays in its group"
        )


def test_the_wrapper_relays_the_compiler_streams_and_status() -> None:
    """Compilers talk to the caller through kache, including when they fail."""
    process = subprocess.run(
        [
            sys.executable,
            str(FIXTURE),
            sys.executable,
            "-c",
            "import sys; print('built'); sys.stderr.write('warning\\n'); sys.exit(7)",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert process.returncode == 7, "the compiler's failure status is relayed verbatim"
    assert process.stdout == "built\n", "standard output is relayed"
    assert process.stderr == "warning\n", "standard error is relayed"


def test_leak_mode_leaves_a_live_process_named_kache(tmp_path: Path) -> None:
    """The leak is the guard's evidence: a kache process no scope accounts for.

    The binary is copied rather than linked, and into a fresh directory, so the
    fixture can present the name the guard matches without ever writing through
    a path that might already be a symlink.
    """
    binary = tmp_path / "kache"
    shutil.copy("/bin/sleep", binary)
    marker = tmp_path / "leaked.pid"
    env = {
        **os.environ,
        "FAKE_KACHE_LEAK_DAEMON": str(marker),
        "FAKE_KACHE_LEAK_BINARY": str(binary),
    }
    process = subprocess.run(
        [sys.executable, str(FIXTURE), sys.executable, "-c", "pass"],
        env=env,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert process.returncode == 0, "the wrapper itself still compiles"
    pid = int(marker.read_text(encoding="ascii"))
    try:
        assert pid_alive(pid), "the leaked daemon outlives the wrapper that started it"
        assert Path(f"/proc/{pid}/exe").resolve().name == "kache", (
            "the guard's evidence is the executable's own name"
        )
    finally:
        os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.02)
    assert not pid_alive(pid), "the recorded pid is the process the caller can remove"
