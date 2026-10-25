"""Bounded statistics queries and server-generation reads."""

import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
from listener_helpers import open_unix_listener, unix_listener

import buildlimits.nonblocking_child as child_api
import buildlimits.outcome_attribution as attribution_api
import buildlimits.outcome_ports as ports_api
from buildlimits.outcome_queries import PeerServerProcess, SccacheStatistics

FIXTURES = Path(__file__).parent / "fixtures"


def _write_proc_entry(
    proc_root: Path,
    *,
    starttime: int,
    environment: bytes,
    missing_file: str | None = None,
) -> None:
    """Write the small proc files needed to identify a listening process."""
    pid = os.getpid()
    process = proc_root / str(pid)
    process.mkdir(parents=True, exist_ok=True)
    fields = ["S", "1", *(["0"] * 17), str(starttime)]
    if missing_file != "stat":
        (process / "stat").write_text(
            f"{pid} (fake server) {' '.join(fields)}\n", encoding="ascii"
        )
    if missing_file != "environ":
        (process / "environ").write_bytes(environment)


def _write_sccache_wrapper(tmp_path: Path) -> Path:
    """Create an executable that routes pinned query argv to the fake client."""
    wrapper = tmp_path / "sccache"
    fixture_directory = str(FIXTURES)
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {fixture_directory!r})\n"
        "from fake_sccache import client\n"
        "sys.exit(client(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def test_statistics_query_uses_pinned_argv_and_filtered_environment(
    tmp_path: Path,
) -> None:
    """Start the fixed stats command with only supervisor-owned variables."""
    process = mock.Mock()
    process.stdout = mock.Mock()
    process.stdout.fileno.return_value = 41
    environment = {
        "PATH": "/usr/bin",
        "HOME": "/home/buildlimits",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "RUSTC": "/private/compiler",
        "SCCACHE_DIR": "/private/cache",
        "INVOCATION_ID": "private-invocation",
    }
    reaper = child_api.ChildReaper()
    with (
        mock.patch(
            "buildlimits.nonblocking_child.subprocess.Popen", return_value=process
        ) as popen,
        mock.patch("buildlimits.nonblocking_child.os.set_blocking") as set_blocking,
    ):
        statistics = SccacheStatistics(
            sys.executable,
            environment,
            tmp_path / "sccache.sock",
            reaper=reaper,
        )
        ticket = statistics.start(10.0)

    assert isinstance(ticket, ports_api.Ticket), (
        "a child start returns its bounded ticket"
    )
    assert ticket.deadline == pytest.approx(12.0), "the query has a two-second deadline"
    assert statistics.start(10.5) == ports_api.QueryFailure("read_error"), (
        "a second query cannot overlap its active ticket"
    )
    popen.assert_called_once_with(
        [sys.executable, "--show-stats", "--stats-format", "json"],
        stdin=mock.ANY,
        stdout=mock.ANY,
        stderr=mock.ANY,
        env={
            "PATH": "/usr/bin",
            "HOME": "/home/buildlimits",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "SCCACHE_SERVER_UDS": str(tmp_path / "sccache.sock"),
        },
        bufsize=0,
        close_fds=True,
    )
    assert "preexec_fn" not in popen.call_args.kwargs, "no pre-exec hook is passed"
    set_blocking.assert_called_once_with(41, False)
    statistics.close(ticket)


def test_query_spawn_failure_is_reported_as_read_error(tmp_path: Path) -> None:
    """A failed helper launch is a bounded reading failure."""
    with mock.patch(
        "buildlimits.nonblocking_child.subprocess.Popen",
        side_effect=OSError("spawn refused"),
    ):
        statistics = SccacheStatistics(
            sys.executable,
            {"PATH": "/usr/bin"},
            tmp_path / "sccache.sock",
        )
        result = statistics.start(10.0)

    assert result == ports_api.QueryFailure("read_error"), (
        "spawn errors use the closed port reason"
    )


def test_fake_sccache_client_returns_statistics_json(tmp_path: Path) -> None:
    """The adapter captures the fixed fixture through its non-blocking child."""
    statistics = SccacheStatistics(
        str(_write_sccache_wrapper(tmp_path)),
        {"PATH": "/usr/bin", "HOME": str(tmp_path)},
        tmp_path / "sccache.sock",
    )
    now = time.monotonic()
    ticket = statistics.start(now)
    assert isinstance(ticket, ports_api.Ticket), (
        "the executable starts a statistics query"
    )

    result: ports_api.Pending | ports_api.StatisticsText | ports_api.QueryFailure = (
        ports_api.Pending()
    )
    while isinstance(result, ports_api.Pending):
        result = statistics.poll(ticket, time.monotonic())
        time.sleep(0.01)

    assert isinstance(result, ports_api.StatisticsText), (
        "the fake client returns JSON text"
    )
    assert json.loads(result.text)["version"] == "0.14.0", (
        "the fixture version reaches the parser unchanged"
    )
    statistics.close(ticket)
    statistics.close(ticket)


@pytest.mark.parametrize(
    ("environment", "expected_invocation", "starttime"),
    [
        (b"PATH=/usr/bin\0INVOCATION_ID=invocation-a\0", "invocation-a", 456),
        (b"PATH=/usr/bin\0", "", 789),
    ],
)
def test_generation_uses_peer_pid_starttime_and_optional_invocation_id(
    tmp_path: Path, environment: bytes, expected_invocation: str, starttime: int
) -> None:
    """Read identity while allowing a readable environment to omit its ID."""
    socket_path = tmp_path / "server.sock"
    proc_root = tmp_path / "proc"
    pid = os.getpid()
    _write_proc_entry(
        proc_root,
        starttime=starttime,
        environment=environment,
    )

    with unix_listener(socket_path):
        generation = PeerServerProcess(socket_path, proc_root=proc_root).generation()

    assert generation == attribution_api.Generation(
        expected_invocation, pid, starttime
    ), "the full server generation identifies the connected process and optional ID"


@pytest.mark.parametrize("missing_file", ["stat", "environ"])
def test_generation_proc_read_failures_are_unavailable(
    tmp_path: Path, missing_file: str
) -> None:
    """Unreadable identity components never become a guessed generation."""
    socket_path = tmp_path / "server.sock"
    proc_root = tmp_path / "proc"
    _write_proc_entry(
        proc_root,
        starttime=456,
        environment=b"INVOCATION_ID=invocation-a\0",
        missing_file=missing_file,
    )

    with unix_listener(socket_path):
        generation = PeerServerProcess(socket_path, proc_root=proc_root).generation()

    assert generation == ports_api.GenerationFailure("generation_unavailable"), (
        "a failed proc read prevents the generation from being trusted"
    )


def test_generation_rejects_an_oversized_process_environment(tmp_path: Path) -> None:
    """The invocation read does not retain an unbounded proc environment."""
    socket_path = tmp_path / "server.sock"
    proc_root = tmp_path / "proc"
    _write_proc_entry(
        proc_root,
        starttime=456,
        environment=b"X=" + b"x" * (64 * 1024),
    )

    with unix_listener(socket_path):
        generation = PeerServerProcess(socket_path, proc_root=proc_root).generation()

    assert generation == ports_api.GenerationFailure("generation_unavailable"), (
        "an oversized environment is refused within the fixed read budget"
    )


def test_refused_generation_connect_means_no_server(tmp_path: Path) -> None:
    """A stale Unix socket that refuses connections has no generation."""
    socket_path = tmp_path / "server.sock"
    open_unix_listener(socket_path).close()

    generation = PeerServerProcess(socket_path).generation()

    assert generation is None, "a refused peer connect means the server is absent"
