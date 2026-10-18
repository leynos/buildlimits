"""Tests for the systemd, sccache and configuration ports."""

import collections.abc as cabc
import os
import socket
import subprocess
import typing as typ
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from buildlimits import bus_properties, config, sccache, systemd
from buildlimits.proc_trees import count_children, parent_pid
from buildlimits.sccache import ManagedSccache, SccacheError
from buildlimits.systemd import SystemdError, UserManager


class FakeRunner:
    """Records commands and answers them from a script."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        """Create a runner with no recorded calls and a fixed scripted result."""
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Record the command and return the scripted result."""
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, self.returncode, self.stdout, self.stderr
        )


class TimeoutRunner:
    """A runner that records the bound each command was given.

    ``subprocess.run`` is replaced wholesale, so the double sees the same
    keyword arguments the real call passes. Only the bound is read; the rest
    are the capture flags the production caller always sets.
    """

    def __init__(self) -> None:
        """Create a runner with nothing recorded and a successful script."""
        self.bounds: list[float] = []

    def __call__(
        self, argv: cabc.Sequence[str], /, **options: object
    ) -> subprocess.CompletedProcess[str]:
        """Record the timeout and answer with a successful empty result."""
        self.bounds.append(typ.cast("float", options["timeout"]))
        return subprocess.CompletedProcess(list(argv), 0, "", "")


class TimeoutRefusingRunner(TimeoutRunner):
    """A runner that times out every command it is given."""

    def __call__(
        self, argv: cabc.Sequence[str], /, **options: object
    ) -> subprocess.CompletedProcess[str]:
        """Raise the timeout a blocked manager connection produces."""
        raise subprocess.TimeoutExpired(
            list(argv), typ.cast("float", options["timeout"])
        )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (systemd.unit_verb_argv("stop", "s.service"), 3),
        (systemd.unit_verb_argv("start", "s.service"), 3),
        ([systemd.SYSTEMCTL, "--user", "show", "s.service", "-p", "MainPID"], 3),
        (systemd.start_scope_argv("u.scope", 42, "s.slice"), 30),
        (
            bus_properties.busctl_argv(
                *bus_properties.control_unit_argv("stop", "s.service")
            ),
            3,
        ),
    ],
    ids=["stop", "start", "show", "scope", "public-control"],
)
def test_every_command_carries_its_own_transport_bound(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected: float,
) -> None:
    """A control command gets the short bound; scope creation gets the long one.

    ``stop`` and ``start`` only queue a job, so a manager that accepts the
    connection and then stalls must not hold the supervisor for the full
    synchronous bound. Creating a scope is a synchronous bus call the manager
    must complete, so it keeps the longer bound.
    """
    runner = TimeoutRunner()
    monkeypatch.setattr(systemd.subprocess, "run", runner)

    systemd.run(argv)

    assert runner.bounds == [expected], (
        "the transport bound matches what the command actually does"
    )


def test_the_public_control_call_passes_only_busctl_options() -> None:
    """Every option in the public control vector is one ``busctl`` accepts.

    ``busctl`` rejects an unknown option outright, before it ever reaches the
    bus, so a ``systemctl`` flag carried across by habit turns the fallback
    into a command that cannot run. That failure is invisible to a runner
    double, which answers whatever it is handed, so this pins the option set
    itself rather than the call's behaviour. ``--expect-reply=no`` is the
    specific trap here: it is a real ``busctl`` option, but it discards the
    job path the caller validates, so it must not appear either.
    """
    argv = bus_properties.busctl_argv(
        *bus_properties.control_unit_argv("stop", "s.service")
    )

    options = [word for word in argv[1:] if word.startswith("--")]
    assert set(options) <= {
        "--timeout=3s",
        "--json=short",
        f"--address=unix:path=/run/user/{os.getuid()}/bus",
    }, "a control call carries only options busctl itself accepts"
    assert "--expect-reply=no" not in argv, (
        "the job path is the caller's evidence, so the reply is expected"
    )
    assert argv[argv.index(options[-1]) + 1] == "call", (
        "the options precede the subcommand rather than trailing the operands"
    )


def test_a_control_timeout_is_a_systemd_error_with_its_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled control connection fails fast and names the command."""
    monkeypatch.setattr(
        systemd.subprocess, "run", TimeoutRefusingRunner(), raising=True
    )

    with pytest.raises(SystemdError, match=r"cannot run .*stop s\.service") as caught:
        systemd.run(systemd.unit_verb_argv("stop", "s.service"))

    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired), (
        "the timeout is carried as the cause rather than stringified"
    )


def test_scopes_are_created_with_the_process_and_slice() -> None:
    """The busctl call names the unit, the process and the slice."""
    runner = FakeRunner()
    UserManager(runner).start_scope("u.scope", 42, "s.slice")
    call = runner.calls[0]
    assert call[:3] == ["busctl", "--user", "call"], "the call goes through busctl"
    assert {"u.scope", "42", "s.slice", "inactive-or-failed"} <= set(call), (
        "the call names the unit, the process and the slice"
    )


def test_manager_failures_raise() -> None:
    """A refused request is an error naming the unit."""
    runner = FakeRunner(returncode=1, stderr="Access denied")
    with pytest.raises(SystemdError, match=r"u\.scope.*Access denied"):
        UserManager(runner).start_scope("u.scope", 1, "s.slice")
    with pytest.raises(SystemdError):
        UserManager(runner).show("x.service", "MainPID")


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError(2, "No such file or directory", "busctl"),
        subprocess.TimeoutExpired(["busctl"], 30),
    ],
    ids=["missing", "timeout"],
)
def test_a_manager_that_cannot_be_reached_is_a_systemd_error(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """A command that cannot start or finish fails like a refusal."""

    def fail(*_args: object, **_kwargs: object) -> None:
        """Fail as the command runner would."""
        raise failure

    monkeypatch.setattr(systemd.subprocess, "run", fail)
    with pytest.raises(SystemdError, match=r"cannot run busctl") as caught:
        UserManager().start_scope("u.scope", 1, "s.slice")
    assert caught.value.__cause__ is failure, "the original failure is the cause"


def test_properties_are_parsed() -> None:
    """``systemctl show`` output becomes a mapping."""
    runner = FakeRunner(stdout="MainPID=7\nControlGroup=/a/b\n")
    assert UserManager(runner).show("x.service", "MainPID", "ControlGroup") == {
        "MainPID": "7",
        "ControlGroup": "/a/b",
    }, "systemctl show output becomes a mapping"


@given(
    st.text(st.characters(exclude_characters="\n"), max_size=20),
    st.integers(1, 2**22),
    st.integers(0, 2**22),
)
def test_parent_pid_survives_hostile_command_names(
    name: str, pid: int, ppid: int
) -> None:
    """Parentheses and spaces in the command name do not confuse parsing."""
    assert parent_pid(f"{pid} ({name}) S {ppid} {pid} {pid} 0 -1") == ppid, (
        "hostile command names do not confuse the parent-pid parser"
    )


def test_only_direct_children_of_the_server_count(tmp_path: Path) -> None:
    """Grandchildren such as linkers are not separate compilations."""
    for pid, ppid in ((100, 1), (101, 100), (102, 100), (103, 101)):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "stat").write_text(f"{pid} (x) S {ppid} 0")
    assert count_children(100, [100, 101, 102, 103, 999], proc=tmp_path) == 2, (
        "grandchildren are not counted as direct children"
    )


@pytest.mark.parametrize(
    ("stat", "error"),
    [(None, IsADirectoryError), ("no command name", ValueError)],
    ids=["unreadable", "malformed"],
)
def test_a_child_that_cannot_be_inspected_is_not_skipped(
    tmp_path: Path, stat: str | None, error: type[Exception]
) -> None:
    """Only a process that has exited is skipped; other failures are raised."""
    (tmp_path / "101").mkdir()
    if stat is None:
        (tmp_path / "101" / "stat").mkdir()
    else:
        (tmp_path / "101" / "stat").write_text(stat)
    with pytest.raises(error):
        count_children(100, [101], proc=tmp_path)


def test_server_load_comes_from_the_unit() -> None:
    """A stopped unit has no load; a failed query propagates."""
    stopped = ManagedSccache(
        "s.service",
        Path("/nonexistent"),
        UserManager(FakeRunner(stdout="MainPID=0\nControlGroup=\n")),
    )
    assert stopped.active_compiles() == 0, "a stopped unit has no load"
    broken = ManagedSccache(
        "s.service", Path("/nonexistent"), UserManager(FakeRunner(returncode=1))
    )
    with pytest.raises(SccacheError, match=r"s\.service") as caught:
        broken.active_compiles()
    assert isinstance(caught.value.__cause__, SystemdError), "the cause is kept"


def test_an_unreadable_server_group_is_an_unknown_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control group that cannot be read is an error, never a zero load."""
    procs = tmp_path / "srv" / "cgroup.procs"
    procs.parent.mkdir()
    # A self-referencing link fails to read with ELOOP, even for root.
    procs.symlink_to("cgroup.procs")
    server = ManagedSccache(
        "s.service",
        Path("/nonexistent"),
        UserManager(FakeRunner(stdout="MainPID=7\nControlGroup=/srv\n")),
    )
    monkeypatch.setattr(
        sccache.cgroups, "directory", lambda path: tmp_path / path.relative_to("/")
    )
    with pytest.raises(SccacheError, match=r"cannot read s\.service's load"):
        server.active_compiles()


def test_reachability_is_a_real_connection(tmp_path: Path) -> None:
    """Only a listening socket counts as a reachable server."""
    path = tmp_path / "s.sock"
    server = ManagedSccache("s.service", path)
    assert not server.is_reachable(), "an absent socket is not reachable"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen(1)
        assert server.is_reachable(), "a listening socket is reachable"


def test_a_probe_fault_is_not_taken_for_a_stopped_server(tmp_path: Path) -> None:
    """A socket path that cannot be probed is an error, not an absent server."""
    (tmp_path / "file").write_text("")
    server = ManagedSccache("s.service", tmp_path / "file" / "s.sock")
    with pytest.raises(SccacheError, match="cannot probe"):
        server.is_reachable()


def test_defaults_load_without_a_file(tmp_path: Path) -> None:
    """A missing file yields the documented defaults."""
    settings = config.load(tmp_path / "absent.toml")
    assert settings.slots == 2, "the default slot count is 2"
    assert settings.cache_mode == "sccache", "the default cache mode is sccache"
    assert settings.compilers_slice == "buildlimits-compilers.slice", (
        "the default compilers slice matches the documented name"
    )


def test_files_override_defaults(tmp_path: Path) -> None:
    """Every setting can come from the file, with types checked."""
    path = tmp_path / "s.toml"
    path.write_text(
        'slots = 3\ndevice = "/dev/x"\ncancel_grace_seconds = 4\ncache_mode = "direct"\n'
    )
    settings = config.load(path)
    assert (settings.slots, settings.device, settings.cancel_grace_seconds) == (
        3,
        Path("/dev/x"),
        4.0,
    ), "every setting is read from the file"
    assert settings.cache_mode == "direct", (
        "the file's cache mode overrides the default"
    )


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("slots = 0\n", "positive"),
        ('cache_mode = "bypass"\n', "cache_mode"),
        ("slots = true\n", "wrong type"),
        ("colour = 1\n", "unknown setting"),
        ('compilers_slice = "x.service"\n', "slice"),
        ("slots = [\n", "cannot read"),
    ],
)
def test_invalid_files_are_rejected(tmp_path: Path, text: str, fragment: str) -> None:
    """Invalid configuration is reported, never half-applied."""
    path = tmp_path / "s.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError, match=fragment):
        config.load(path)


def test_sockets_live_under_the_runtime_directory() -> None:
    """Socket paths stay short by living under XDG_RUNTIME_DIR."""
    assert config.supervisor_socket({"XDG_RUNTIME_DIR": "/run/user/5"}) == Path(
        "/run/user/5/buildlimits/supervisor.sock"
    ), "the socket sits under XDG_RUNTIME_DIR by default"
    assert config.supervisor_socket({"BUILD_LIMITS_SOCKET": "/x.sock"}) == Path(
        "/x.sock"
    ), "an explicit socket path overrides the default"
    assert config.runtime_dir({}) == Path(f"/run/user/{os.getuid()}/buildlimits"), (
        "the runtime directory falls back to the uid-based default"
    )


def test_server_group_orphans_remain_accounted_for(tmp_path: Path) -> None:
    """A surviving orphan root needs a token even when it has lost its server."""
    for pid, ppid in ((100, 1), (101, 1), (102, 101)):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "stat").write_text(f"{pid} (compiler) S {ppid} 0")
    assert count_children(100, [100, 101, 102], proc=tmp_path) == 1, (
        "one orphan compiler tree stays covered until it drains"
    )
