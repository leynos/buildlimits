"""Dispatch safe rustc queries outside compile admission."""

import typing as typ
from unittest import mock

import pytest

from buildlimits import adapter, adapter_metrics

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _ExecedError(Exception):
    """Stand in for process replacement by ``os.execvpe``."""


class _RecordingJournal:
    """Keep the adapter record and dispatch order for assertions."""

    def __init__(self, events: list[str]) -> None:
        """Bind an event list shared with the fake exec."""
        self.events = events
        self.records: list[dict[str, str]] = []

    def emit(self, record: cabc.Mapping[str, str]) -> None:
        """Keep one record and mark when emission happened."""
        self.records.append(dict(record))
        self.events.append("metric")

    def close(self) -> None:
        """Record release of the adapter-owned journal transport."""
        self.events.append("close")


def _install_journal(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> _RecordingJournal:
    """Replace the native socket with an ordered in-memory sink."""
    journal = _RecordingJournal(events)
    monkeypatch.setattr(adapter_metrics, "JournalSink", lambda **_kwargs: journal)
    return journal


def test_query_dispatch_strips_sccache_and_preserves_driver_and_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A safe wrapped query records exemption before directly execing the driver."""
    events: list[str] = []
    journal = _install_journal(monkeypatch, events)
    effective_env = {"PATH": "/tool/bin", "QUERY_TEST": "same mapping"}
    monkeypatch.setattr(
        adapter, "toolchain_environment", lambda _argv, _env, _cwd: effective_env
    )
    monkeypatch.setattr(adapter, "in_admitted_scope", lambda _env: False)

    forbidden = mock.Mock(
        side_effect=AssertionError("exempt query must bypass admission")
    )
    monkeypatch.setattr(adapter, "run", forbidden)
    monkeypatch.setattr(adapter, "connect", forbidden)
    execvpe = mock.Mock(side_effect=_ExecedError)
    monkeypatch.setattr(adapter.os, "execvpe", execvpe)
    with pytest.raises(_ExecedError):
        adapter._main(
            ["sccache", "clippy-driver", "rustc", "-vV"],
            cacheable=True,
            exempt_queries=True,
        )

    command, argv, env = execvpe.call_args.args
    assert (command, argv) == (
        "clippy-driver",
        ("clippy-driver", "rustc", "-vV"),
    ), "the caller cache client is removed while the verified driver remains"
    assert env is effective_env, "the effective environment is shared with exec"
    assert events == ["metric", "close"], "summary is recorded before failed exec"
    assert execvpe.call_count == 1, "the verified compiler receives the query"
    assert len(journal.records) == 1, "one exemption record is emitted"
    assert journal.records[0]["BUILD_LIMITS_OUTCOME"] == "exempt", (
        "the summary records the exemption"
    )


def test_admitted_scope_keeps_precedence_over_query_exemption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Already-admitted work keeps its original argv and emits no summary."""
    argv = ["rustc", "-vV"]
    monkeypatch.setattr(adapter, "in_admitted_scope", lambda _env: True)
    monkeypatch.setattr(
        adapter,
        "is_toolchain_query",
        pytest.fail,
    )
    monkeypatch.setattr(
        adapter,
        "AdapterMetrics",
        pytest.fail,
    )
    execvpe = mock.Mock(side_effect=_ExecedError)
    monkeypatch.setattr(adapter.os, "execvpe", execvpe)
    with pytest.raises(_ExecedError):
        adapter._main(argv, cacheable=True, exempt_queries=True)
    command, args, _env = execvpe.call_args.args
    assert (command, tuple(args)) == ("rustc", tuple(argv)), (
        "the admitted path preserves the original arguments"
    )


@pytest.mark.parametrize(
    ("exempt_queries", "argv"),
    [
        (False, ["rustc", "-vV"]),
        (True, ["rustc", "-vV", "lib.rs"]),
    ],
    ids=["default-disabled", "unsafe-query-falls-through"],
)
def test_unexempt_invocations_use_normal_admission(
    monkeypatch: pytest.MonkeyPatch, exempt_queries: bool, argv: list[str]
) -> None:
    """The default and every classifier near miss use the admitted runner."""
    monkeypatch.setattr(adapter, "in_admitted_scope", lambda _env: False)
    captured: list[adapter.Invocation] = []

    def record(invocation: adapter.Invocation) -> int:
        """Capture the request rather than opening a supervisor connection."""
        captured.append(invocation)
        return 17

    monkeypatch.setattr(adapter, "run", record)
    assert adapter._main(argv, cacheable=True, exempt_queries=exempt_queries) == 17, (
        "normal admission result passes through"
    )
    assert len(captured) == 1 and captured[0].argv == argv, (
        "the original argv reaches the normal runner"
    )


def test_exempt_exec_failure_keeps_diagnostic_and_emits_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed direct exec retains its error and does not emit a second summary."""
    events: list[str] = []
    journal = _install_journal(monkeypatch, events)
    monkeypatch.setattr(
        adapter,
        "toolchain_environment",
        lambda _argv, _env, _cwd: {"PATH": "/tool/bin"},
    )
    monkeypatch.setattr(adapter, "in_admitted_scope", lambda _env: False)

    forbidden = mock.Mock(
        side_effect=AssertionError("failed query exec must not admit")
    )
    monkeypatch.setattr(adapter, "run", forbidden)
    monkeypatch.setattr(adapter, "connect", forbidden)
    execvpe = mock.Mock(side_effect=FileNotFoundError(2, "No such file or directory"))
    monkeypatch.setattr(adapter.os, "execvpe", execvpe)
    assert (
        adapter._main(["rustc", "-vV"], cacheable=True, exempt_queries=True)
        == adapter.FAILURE_STATUS
    ), "the existing exec failure status is retained"
    assert capsys.readouterr().err.startswith("build-limits: "), (
        "the error keeps the build-limits prefix"
    )
    assert len(journal.records) == 1 and events.count("metric") == 1, (
        "exec failure does not emit a second summary"
    )
    assert execvpe.call_count == 1, "the missing compiler was attempted once"
