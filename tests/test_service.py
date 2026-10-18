"""Tests for the service entry point and its journal diagnostics."""

import os
import signal
from pathlib import Path, PurePosixPath

import pytest

from buildlimits import attributes, cgroups, journal, service, startup
from buildlimits.attributes import ProcessAttributes
from buildlimits.protocol import CompileRequest
from buildlimits.requesters import Pending
from buildlimits.sccache import SccacheError
from buildlimits.systemd import SystemdError

OWN_CGROUP = PurePosixPath(
    "/user.slice/user-1000.slice/user@1000.service/app.slice/s.service"
)


def _write_config(tmp_path: Path, *, lock_path: Path | None = None) -> Path:
    """Write a valid supervisor TOML config scoped under tmp_path."""
    resolved_lock = lock_path if lock_path is not None else tmp_path / "lock"
    config_path = tmp_path / "supervisor.toml"
    config_path.write_text(f'lock_path = "{resolved_lock}"\n', encoding="utf-8")
    return config_path


def _lock_is_free(lock_path: Path) -> bool:
    """Report whether the singleton lock can be taken again."""
    try:
        fd = startup.acquire_lock(lock_path)
    except startup.StartupError:
        return False
    os.close(fd)
    return True


class _FakeSupervisor:
    """A supervisor stand-in that records calls instead of doing real work."""

    def __init__(self, settings: object) -> None:
        """Remember the settings the service constructed this instance with."""
        self.settings = settings
        self.start_calls: list[PurePosixPath] = []
        self.serve_calls = 0
        self.stop_requested = False
        self.start_error: BaseException | None = None
        self.serve_status = 0

    def start(self, own_cgroup: PurePosixPath) -> None:
        """Record the start-up call, raising the configured error if any."""
        self.start_calls.append(own_cgroup)
        if self.start_error is not None:
            raise self.start_error

    def serve(self) -> int:
        """Record a serve call and return the configured exit status."""
        self.serve_calls += 1
        return self.serve_status

    def request_stop(self) -> None:
        """Record that an orderly shutdown was requested."""
        self.stop_requested = True


def _install_fake_supervisor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    start_error: BaseException | None = None,
    serve_status: int = 0,
) -> list[_FakeSupervisor]:
    """Patch service.Supervisor with a fake factory and return created instances."""
    created: list[_FakeSupervisor] = []

    def factory(settings: object, **_kwargs: object) -> _FakeSupervisor:
        """Build and record one fake supervisor for this call."""
        instance = _FakeSupervisor(settings)
        instance.start_error = start_error
        instance.serve_status = serve_status
        created.append(instance)
        return instance

    monkeypatch.setattr(service, "Supervisor", factory)
    return created


def test_an_invalid_config_file_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed configuration file fails start-up with exit status 2."""
    config_path = tmp_path / "supervisor.toml"
    config_path.write_text('slots = "not a number"\n', encoding="utf-8")

    status = service.main(["--config", str(config_path)])

    assert status == 2, "a configuration error is not restartable"
    err = capsys.readouterr().err
    assert "build-limits-supervisor:" in err, err
    assert "slots" in err, err


def test_a_held_singleton_lock_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lock already held by another supervisor fails start-up with exit status 2."""
    lock_path = tmp_path / "lock"
    config_path = _write_config(tmp_path, lock_path=lock_path)
    held_fd = startup.acquire_lock(lock_path)
    try:
        status = service.main(["--config", str(config_path)])
    finally:
        os.close(held_fd)

    assert status == 2, "a held lock is not restartable"
    assert "another supervisor holds" in capsys.readouterr().err, "the lock is named"


def test_an_undeterminable_cgroup_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unreadable control group fails start-up with exit status 2."""
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(cgroups, "own_path", lambda: None)

    status = service.main(["--config", str(config_path)])

    assert status == 2, "an undeterminable cgroup is not restartable"
    assert "cannot determine" in capsys.readouterr().err, "the cgroup is named"
    assert _lock_is_free(tmp_path / "lock"), "the lock is released"


@pytest.mark.parametrize(
    "error",
    [
        startup.StartupError("compiler scopes did not drain within 1.0s"),
        OSError("no such device"),
        SystemdError("the user manager rejected a request"),
        SccacheError("the managed server's state is unknown"),
    ],
    ids=["startup", "os", "systemd", "sccache"],
)
def test_a_failed_start_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
) -> None:
    """Every error start() can raise fails start-up with exit status 1."""
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(cgroups, "own_path", lambda: OWN_CGROUP)
    _install_fake_supervisor(monkeypatch, start_error=error)

    status = service.main(["--config", str(config_path)])

    assert status == 1, "a failed start is restartable"
    err = capsys.readouterr().err
    assert "start-up failed" in err, err
    assert str(error) in err, err
    assert _lock_is_free(tmp_path / "lock"), "the lock is released"


def test_a_successful_start_serves_and_closes_the_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A clean start returns serve()'s status and closes the lock descriptor."""
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(cgroups, "own_path", lambda: OWN_CGROUP)
    created = _install_fake_supervisor(monkeypatch, serve_status=7)
    closed_fds: list[int] = []
    real_close = os.close

    def _tracking_close(fd: int) -> None:
        """Record a closed descriptor, then close it for real."""
        closed_fds.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "close", _tracking_close)

    status = service.main(["--config", str(config_path)])

    assert status == 7, "main returns serve()'s status"
    assert len(created) == 1, "exactly one supervisor was constructed"
    supervisor = created[0]
    assert supervisor.start_calls == [OWN_CGROUP], "start was called with own cgroup"
    assert supervisor.serve_calls == 1, "serve was called exactly once"
    assert len(closed_fds) == 1, "the lock descriptor was closed exactly once"
    capsys.readouterr()


def test_a_successful_start_wires_sigterm_and_sigint_to_request_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SIGTERM and SIGINT handlers both call the supervisor's request_stop."""
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(cgroups, "own_path", lambda: OWN_CGROUP)
    created = _install_fake_supervisor(monkeypatch)
    original_term = signal.getsignal(signal.SIGTERM)
    original_int = signal.getsignal(signal.SIGINT)
    try:
        service.main(["--config", str(config_path)])
        term_handler = signal.getsignal(signal.SIGTERM)
        int_handler = signal.getsignal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGTERM, original_term)
        signal.signal(signal.SIGINT, original_int)
    capsys.readouterr()

    supervisor = created[0]
    # Neither a default nor an ignored disposition: a Python handler.
    assert term_handler is not None, "a SIGTERM handler was installed"
    assert not isinstance(term_handler, int), "a SIGTERM handler was installed"
    assert int_handler is not None, "a SIGINT handler was installed"
    assert not isinstance(int_handler, int), "a SIGINT handler was installed"
    assert not supervisor.stop_requested, "no stop was requested before a signal"
    term_handler(signal.SIGTERM, None)
    assert supervisor.stop_requested, "SIGTERM is wired to request_stop"
    supervisor.stop_requested = False
    int_handler(signal.SIGINT, None)
    assert supervisor.stop_requested, "SIGINT is wired to request_stop"


def test_log_writes_one_prefixed_line_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Log writes one prefixed diagnostic line to stderr."""
    journal.log("hello")

    assert capsys.readouterr().err == "build-limits-supervisor: hello\n", (
        "one prefixed line is written"
    )


def _pending(*, group_id: str = "g0", admitted: float | None = 1.5) -> Pending:
    """Build an admitted request whose label differs from its arguments."""
    request = CompileRequest(
        argv=("/usr/bin/rustc", "--edition=2021", "--crate-name=widget"),
        cwd="/home/build/widget",
        env={},
        attributes=ProcessAttributes(umask=0o22, nice=0, rlimits={}),
        fd_targets=(),
        label="widget",
        cacheable=True,
    )
    plan = attributes.AttributePlan(umask=0o22, nice=0, rlimits={})
    entry = Pending(7, None, request, [], plan, arrived=0.0)
    entry.group_id = group_id
    entry.admitted = admitted
    return entry


@pytest.mark.parametrize(
    ("cached", "suffix"),
    [(True, " via sccache"), (False, "")],
    ids=["cached", "direct"],
)
def test_admitted_line_describes_the_admission_without_arguments(
    *, cached: bool, suffix: str
) -> None:
    """The line names the request, group and program, never argv or a path."""
    line = journal.admitted_line(_pending(), cached=cached)
    assert line == f"admitted request 7 as g0 after 1.5s: rustc{suffix}", (
        "the admission is described"
    )
    assert "--crate-name" not in line, "compiler arguments must not be logged"
    assert "/home/build" not in line, "the requester's directory must not be logged"
