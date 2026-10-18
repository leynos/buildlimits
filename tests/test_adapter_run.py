"""Tests for the adapter's request/reply exchange, ``_main`` and entry points.

Scope detection, rustup lookup, connection waiting, exit reproduction and
signal cancellation are covered in ``test_adapter.py``.
"""

import socket
import typing as typ

import pytest

from buildlimits import adapter, adapter_metrics
from buildlimits.adapter import AdapterError
from buildlimits.attributes import AttributeCaptureError
from buildlimits.framing import ProtocolError, encode_frame
from buildlimits.metrics import Metrics
from buildlimits.protocol import (
    Admitted,
    Exit,
    Finished,
    Queued,
    Refused,
    encode_reply,
)
from buildlimits.rustdoc import RustdocError
from buildlimits.timing import Clock, RetryWait

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


class _ExecedError(Exception):
    """Raised by a faked ``os.execvp`` instead of replacing the process."""


def _no_fds(exclude: cabc.Collection[int]) -> list[int]:
    """Return no inheritable descriptors, keeping requests free of real fds."""
    return []


@pytest.fixture
def supervisor_end(
    monkeypatch: pytest.MonkeyPatch,
    socket_pair: tuple[socket.socket, socket.socket],
) -> socket.socket:
    """Route ``run`` to the fake connection and return the supervisor's end."""
    client, server = socket_pair

    def connect(
        _path: Path, _deadline: float, _heartbeat: object, dialler: adapter.Dialler
    ) -> socket.socket:
        """Return the fake connection after counting two attempts.

        Returns
        -------
        socket.socket
            The connected client.
        """
        assert dialler.attempted is not None, "attempt observer installed"
        dialler.attempted()
        dialler.attempted()
        return client

    monkeypatch.setattr(adapter, "connect", connect)
    monkeypatch.setattr(adapter.descriptors, "inheritable_descriptors", _no_fds)
    return server


# --- run: the request/reply exchange -----------------------------------------


def test_run_returns_the_exit_code_after_queued_and_admitted(
    supervisor_end: socket.socket, capsys: pytest.CaptureFixture[str], metric_sink
) -> None:
    """Queued, Admitted and Finished replies yield the compiler's exit code."""
    supervisor_end.sendall(encode_reply(Queued(position=2)))
    supervisor_end.sendall(
        encode_reply(Admitted(group="g1", cached=True, notice="no cache yet"))
    )
    supervisor_end.sendall(encode_reply(Finished(exit=Exit(code=17))))
    exit_code = adapter.run(
        ["rustc", "--crate-name", "demo", "x.rs"],
        cacheable=True,
        metrics=Metrics(metric_sink, source="adapter"),
        retry=RetryWait(clock=Clock(now=lambda: 1.0)),
    )
    assert exit_code == 17, "the Finished reply's exit code passes through"
    output = capsys.readouterr()
    assert not output.out, "the adapter writes no compiler stdout"
    assert output.err == "[build-limits] demo: no cache yet\n", "notice unchanged"
    assert len(metric_sink.records) == 1, "one invocation summary"
    record = metric_sink.records[0]
    assert record["BUILD_LIMITS_SOURCE"] == "adapter", "self-reported source"
    assert record["BUILD_LIMITS_OUTCOME"] == "completed", "compiler result received"
    assert record["BUILD_LIMITS_CONNECTION_ATTEMPTS"] == "2", "retries counted"
    assert "demo" not in str(record) and "x.rs" not in str(record), "no identifiers"


@pytest.mark.parametrize(
    ("frames", "error", "message"),
    [
        (
            [encode_reply(Refused(reason="pool-closed", message="no slots left"))],
            AdapterError,
            "no slots left",
        ),
        (
            [encode_reply(Queued(0)), encode_reply(Admitted("g1", False))],
            AdapterError,
            "closed the connection",
        ),
        ([encode_frame({"type": "nonsense"})], ProtocolError, "unknown reply type"),
    ],
    ids=["refused", "closed-after-admission", "malformed"],
)
def test_run_fails_without_a_finished_reply(
    supervisor_end: socket.socket,
    frames: list[bytes],
    error: type[Exception],
    message: str,
) -> None:
    """A refusal, a close after admission or a malformed reply each fail the exchange."""
    for frame in frames:
        supervisor_end.sendall(frame)
    supervisor_end.shutdown(socket.SHUT_WR)
    with pytest.raises(error, match=message):
        adapter.run(["rustc", "x.rs"], cacheable=True)


# --- _main --------------------------------------------------------------------


def test_main_rejects_an_empty_argument_vector(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No compiler named at all is a usage error, not a build-limits failure."""
    assert adapter._main([], cacheable=True) == adapter.USAGE_STATUS, (
        "an empty argv is a usage error"
    )
    assert "no compiler given" in capsys.readouterr().err, (
        "the usage error explains what is missing"
    )


def _admitted_with_exec(
    monkeypatch: pytest.MonkeyPatch, execvp: cabc.Callable[[str, list[str]], None]
) -> None:
    """Run ``_main`` as if already admitted, with ``run`` forbidden."""
    monkeypatch.setattr(adapter, "in_admitted_scope", lambda env: True)
    monkeypatch.setattr(adapter.os, "execvp", execvp)

    def fail_run(*_a: object, **_kw: object) -> int:
        """Fail the test if the request/reply exchange is reached."""
        msg = "run() must not be called when already admitted"
        raise AssertionError(msg)

    monkeypatch.setattr(adapter, "run", fail_run)


def test_main_executes_directly_when_already_admitted(
    monkeypatch: pytest.MonkeyPatch, metric_sink
) -> None:
    """Inside an admitted scope, the compiler runs directly, bypassing run()."""

    def fake_execvp(cmd: str, args: list[str]) -> typ.NoReturn:
        """Stand in for process replacement by raising a marker exception."""
        raise _ExecedError((cmd, tuple(args)))

    monkeypatch.setattr(
        adapter_metrics,
        "Metrics",
        lambda *_args, **_kwargs: Metrics(metric_sink, source="adapter"),
    )
    _admitted_with_exec(monkeypatch, fake_execvp)
    with pytest.raises(_ExecedError) as caught:
        adapter._main(["rustc", "a.rs"], cacheable=True)
    assert metric_sink.records == [], "nested exec emits no adapter summary"
    assert caught.value.args[0] == ("rustc", ("rustc", "a.rs")), (
        "the compiler is exec'd with its own argument vector"
    )


def test_main_reports_a_compiler_that_cannot_be_executed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed direct exec inside an admitted scope is an adapter failure."""

    def missing(cmd: str, args: list[str]) -> typ.NoReturn:
        """Fail as exec does for a compiler that is not installed."""
        raise FileNotFoundError(2, "No such file or directory", cmd)

    _admitted_with_exec(monkeypatch, missing)
    status = adapter._main(["rustc", "a.rs"], cacheable=True)
    assert status == adapter.FAILURE_STATUS, "the adapter fails cleanly"
    assert capsys.readouterr().err.startswith("build-limits: "), (
        "the failure is reported like any other adapter failure"
    )


@pytest.mark.parametrize(
    "error",
    [
        AdapterError("supervisor refused the request"),
        AttributeCaptureError("cannot read the umask"),
        ProtocolError("unknown reply type"),
        OSError("broken pipe"),
    ],
    ids=["adapter", "attribute-capture", "protocol", "os"],
)
def test_main_reports_known_failures_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    """Every documented failure from run() becomes a labelled stderr line."""
    monkeypatch.setattr(adapter, "in_admitted_scope", lambda env: False)

    def fail_run(*_a: object, **_kw: object) -> int:
        """Simulate the scripted failure from the request/reply exchange."""
        raise error

    monkeypatch.setattr(adapter, "run", fail_run)
    assert adapter._main(["rustc", "a.rs"], cacheable=True) == adapter.FAILURE_STATUS, (
        "a known failure yields the adapter's own failure status"
    )
    assert f"build-limits: {error}" in capsys.readouterr().err, (
        "the failure message is written to stderr"
    )


# --- rustc_main and rustdoc_main -----------------------------------------------


@pytest.mark.parametrize(
    ("entry", "argv", "expected"),
    [
        (
            adapter.rustc_main,
            ["build-limits-rustc", "rustc", "-vV"],
            {"argv": ["rustc", "-vV"], "cacheable": True},
        ),
        (
            adapter.rustdoc_main,
            ["build-limits-rustdoc", "--help"],
            {"argv": ["/real/rustdoc", "--help"], "cacheable": False},
        ),
    ],
    ids=["rustc", "rustdoc"],
)
def test_entry_points_hand_their_compiler_to_main(
    monkeypatch: pytest.MonkeyPatch,
    entry: cabc.Callable[[], int],
    argv: list[str],
    expected: dict[str, object],
) -> None:
    """Rustc runs cacheably as given; rustdoc runs the resolved binary uncached."""
    monkeypatch.setattr(adapter.sys, "argv", argv)
    monkeypatch.setattr(adapter, "resolve_rustdoc", lambda env, cwd: "/real/rustdoc")
    captured: dict[str, object] = {}

    def fake_main(argv: list[str], *, cacheable: bool) -> int:
        """Record the arguments _main received."""
        captured.update(argv=argv, cacheable=cacheable)
        return 7

    monkeypatch.setattr(adapter, "_main", fake_main)
    assert entry() == 7, "the result of _main is returned unchanged"
    assert captured == expected, "the entry point passes its compiler and cache mode"


def test_rustdoc_main_fails_when_rustdoc_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A rustup resolution failure is reported without reaching ``_main``."""

    def fail(env: cabc.Mapping[str, str], cwd: str) -> str:
        """Simulate a rustup that cannot resolve rustdoc."""
        msg = "rustup cannot resolve rustdoc: no toolchain"
        raise RustdocError(msg)

    monkeypatch.setattr(adapter, "resolve_rustdoc", fail)
    assert adapter.rustdoc_main() == adapter.FAILURE_STATUS, (
        "resolution failure is the adapter's own failure status"
    )
    assert "rustup cannot resolve rustdoc" in capsys.readouterr().err, (
        "the resolution failure is explained on stderr"
    )


def test_rustdoc_main_reports_a_rustup_that_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A rustup that cannot be executed is a build-limits failure, not a crash."""
    rustup = tmp_path / "rustup"
    rustup.write_text("not a program\n")  # present, but not executable
    monkeypatch.setenv("BUILD_LIMITS_RUSTUP", str(rustup))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(adapter.sys, "argv", ["build-limits-rustdoc", "--help"])
    assert adapter.rustdoc_main() == adapter.FAILURE_STATUS, "the adapter fails"
    assert "build-limits: cannot run rustup" in capsys.readouterr().err, (
        "the failure names rustup"
    )
