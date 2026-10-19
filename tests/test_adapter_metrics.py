"""Silent adapter summaries across failures, durations and signal reproduction."""

import signal
import socket
import typing as typ

import pytest
from descriptor_helpers import open_fds

from buildlimits import adapter, adapter_metrics
from buildlimits.adapter_metrics import AdapterMetrics
from buildlimits.journal_native import JournalSink
from buildlimits.metrics import Metrics
from buildlimits.protocol import Admitted, Exit, Finished, Refused, encode_reply
from buildlimits.timing import Clock, RetryWait

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from conftest import RecordingSink


@pytest.mark.parametrize("failure", ["none", "clock", "sink"])
def test_owned_summary_sink_closes_on_every_finish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    """An owned native socket is released without relying on garbage collection."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as journal:
        path = tmp_path / "journal"
        journal.bind(str(path))
        journal.settimeout(0.05)
        baseline = open_fds()
        sink = JournalSink(path, identifier="build-limits-adapter")
        sink.emit({"BUILD_LIMITS_EVENT": "adapter_finished"})
        journal.recv(8192)
        monkeypatch.setattr(adapter_metrics, "JournalSink", lambda **_kwargs: sink)

        def clock() -> float:
            """Fail an injected clock only in its named regression case."""
            if failure == "clock":
                msg = "clock failed"
                raise RuntimeError(msg)
            return 1.0

        def reject(_record: cabc.Mapping[str, str]) -> typ.NoReturn:
            """Fail emission after the native socket has already been opened."""
            msg = "sink failed"
            raise OSError(msg)

        if failure == "sink":
            monkeypatch.setattr(sink, "emit", reject)
        summary = AdapterMetrics(clock, 0.0)
        try:
            summary.finish()
            summary.finish()
            assert open_fds() == baseline, "owned journal descriptor is closed promptly"
            if failure == "none":
                assert b"BUILD_LIMITS_EVENT=adapter_finished\n" in journal.recv(8192), (
                    "successful finalization sends one native summary"
                )
            with pytest.raises(TimeoutError):
                journal.recv(8192)
        finally:
            sink.close()


def test_injected_summary_sink_remains_owned_by_caller(tmp_path: Path) -> None:
    """Finalizing an injected metrics port does not close its native transport."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as journal:
        path = tmp_path / "journal"
        journal.bind(str(path))
        journal.settimeout(0.05)
        sink = JournalSink(path)
        try:
            sink.emit({"BUILD_LIMITS_EVENT": "adapter_finished"})
            journal.recv(8192)
            baseline = open_fds()
            summary = AdapterMetrics(lambda: 1.0, 0.0, Metrics(sink, source="adapter"))
            summary.finish()
            assert open_fds() == baseline, "the caller retains its injected descriptor"
            assert b"BUILD_LIMITS_EVENT=adapter_finished\n" in journal.recv(8192), (
                "the injected port still receives the summary"
            )
        finally:
            sink.close()


def test_adapter_duration_boundaries(metric_sink: RecordingSink) -> None:
    """Adapter wait ends at the reply; runtime ends at the compiler result."""
    summary = AdapterMetrics(lambda: 9.0, 1.0, Metrics(metric_sink, source="adapter"))
    summary.attempted()
    summary.attempted()
    summary.admission(4.0)
    summary.admission(6.0)
    summary.outcome = "completed"
    summary.finish()
    summary.finish()
    assert len(metric_sink.records) == 1, "terminal emission is idempotent"
    record = metric_sink.records[0]
    assert record["BUILD_LIMITS_WAIT_SECONDS"] == "3.000", "first admission boundary"
    assert record["BUILD_LIMITS_RUNTIME_SECONDS"] == "5.000", "runtime boundary"
    assert record["BUILD_LIMITS_CONNECTION_ATTEMPTS"] == "2", "retry count"


def _connected_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[socket.socket, socket.socket]:
    """Connect the adapter to an in-process supervisor end with nothing to pass.

    Returns
    -------
    tuple[socket.socket, socket.socket]
        The adapter's end and the scripted supervisor's end, both owned by the
        caller.
    """
    client, server = socket.socketpair()
    monkeypatch.setattr(adapter, "connect", lambda *_args: client)
    monkeypatch.setattr(
        adapter.descriptors, "inheritable_descriptors", lambda **_kw: []
    )
    return client, server


@pytest.mark.parametrize("ending", ["refused", "supervisor_lost", "interrupted"])
def test_adapter_failure_summaries(
    monkeypatch: pytest.MonkeyPatch, metric_sink: RecordingSink, ending: str
) -> None:
    """Refusal, loss after admission and interruption each emit one summary."""
    client, server = _connected_adapter(monkeypatch)
    match ending:
        case "refused":
            server.sendall(encode_reply(Refused("unhealthy", "existing refusal text")))
        case "supervisor_lost":
            # Lost after admission: before it the request would be resubmitted.
            server.sendall(encode_reply(Admitted("g1", False)))
        case "interrupted":

            def interrupt(*_args: object, **_kwargs: object) -> typ.NoReturn:
                """Interrupt the reply wait as the interpreter would."""
                raise KeyboardInterrupt

            monkeypatch.setattr(adapter, "_next_reply", interrupt)
    server.shutdown(socket.SHUT_WR)
    try:
        error = KeyboardInterrupt if ending == "interrupted" else adapter.AdapterError
        with pytest.raises(error):
            adapter.run(
                adapter.Invocation(["rustc"], cacheable=True),
                metrics=Metrics(metric_sink, source="adapter"),
            )
    finally:
        server.close()
        client.close()
    assert len(metric_sink.records) == 1, "one terminal record on failure"
    assert metric_sink.records[0]["BUILD_LIMITS_OUTCOME"] == ending, "classified ending"


def test_connection_deadline_summary(
    monkeypatch: pytest.MonkeyPatch, metric_sink: RecordingSink
) -> None:
    """An unavailable supervisor yields the existing error and a timeout summary."""

    def unavailable(
        _path: object, _deadline: float, _heartbeat: object, dialler: adapter.Dialler
    ) -> typ.NoReturn:
        """Count attempts then model the retry loop's terminal deadline."""
        assert dialler.attempted is not None, "attempt observer installed"
        dialler.attempted()
        dialler.attempted()
        msg = "existing unavailable diagnostic"
        raise adapter.AdapterError(msg)

    monkeypatch.setattr(adapter, "connect", unavailable)
    with pytest.raises(adapter.AdapterError, match="existing unavailable diagnostic"):
        adapter.run(
            adapter.Invocation(["rustc"], cacheable=True),
            metrics=Metrics(metric_sink, source="adapter"),
        )
    record = metric_sink.records[0]
    assert record["BUILD_LIMITS_OUTCOME"] == "connect_timeout", "deadline classified"
    assert record["BUILD_LIMITS_CONNECTION_ATTEMPTS"] == "2", "attempts retained"


def test_metric_failure_preserves_result_and_streams(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Even a failing injected port cannot alter compiler status or diagnostics."""

    class BrokenMetrics:
        """A port whose emission always fails."""

        def emit(self, event: str, **_fields: object) -> typ.NoReturn:
            """Fail with private text which must never reach stderr."""
            del event
            msg = "private metric failure"
            raise RuntimeError(msg)

    client, server = _connected_adapter(monkeypatch)
    server.sendall(encode_reply(Admitted("g", False, "existing fallback notice")))
    server.sendall(encode_reply(Finished(Exit(code=17))))
    try:
        assert (
            adapter.run(
                adapter.Invocation(["rustc"], cacheable=True), metrics=BrokenMetrics()
            )
            == 17
        ), "compiler exit status survives metric failure"
    finally:
        client.close()
        server.close()
    output = capsys.readouterr()
    assert not output.out, "compiler stdout is untouched"
    assert output.err == "[build-limits] rustc: existing fallback notice\n", (
        "stderr unchanged"
    )


def test_signal_summary_precedes_reproduction(
    monkeypatch: pytest.MonkeyPatch, metric_sink: RecordingSink
) -> None:
    """A signal record is emitted before the original signal kills the adapter."""
    from buildlimits.adapter_signals import cancel_on_signal

    summary = AdapterMetrics(lambda: 2.0, 1.0, Metrics(metric_sink, source="adapter"))
    handlers: dict[int, object] = {}
    monkeypatch.setattr(adapter.signal, "signal", handlers.__setitem__)
    kills: list[tuple[int, int, int]] = []
    monkeypatch.setattr(
        adapter.os,
        "kill",
        lambda pid, number: kills.append((pid, number, len(metric_sink.records))),
    )
    cancel_on_signal(summary.interrupted)
    handler = typ.cast("cabc.Callable[[int, object], None]", handlers[signal.SIGTERM])
    assert callable(handler), "termination handler registered"
    handler(signal.SIGTERM, None)
    summary.finish()
    assert kills == [(adapter.os.getpid(), signal.SIGTERM, 1)], "emit before signal"
    assert handlers[signal.SIGTERM] == signal.SIG_DFL, "default disposition restored"
    assert len(metric_sink.records) == 1, "exactly one interruption record"
    assert metric_sink.records[0]["BUILD_LIMITS_OUTCOME"] == "interrupted", (
        "interruption classified"
    )


def test_heartbeat_output_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    metric_sink: RecordingSink,
) -> None:
    """Metrics leave both waiting and compiling heartbeat lines unchanged."""
    client, server = _connected_adapter(monkeypatch)
    monkeypatch.setenv("BUILD_LIMITS_HEARTBEAT_SECS", "1")
    now = [0.0]
    replies = iter((Admitted("g", False), Finished(Exit(code=0))))

    def next_reply(
        _conn: object, heartbeat: adapter.Heartbeat, **_kwargs: object
    ) -> object:
        """Emit the scheduled heartbeat before each scripted reply.

        Returns
        -------
        object
            An admission followed by completion.
        """
        now[0] += 2.0
        adapter._stderr(heartbeat.tick())
        return next(replies)

    monkeypatch.setattr(adapter, "_next_reply", next_reply)
    try:
        assert (
            adapter.run(
                adapter.Invocation(["rustc"], cacheable=True),
                retry=RetryWait(clock=Clock(now=lambda: now[0])),
                metrics=Metrics(metric_sink, source="adapter"),
            )
            == 0
        ), "compiler completes with metrics enabled"
    finally:
        client.close()
        server.close()
    assert capsys.readouterr().err == (
        "[build-limits] rustc: waiting for a compile slot (0 ahead, 2s)\n"
        "[rustc-heartbeat] rustc: still compiling after 2s\n"
    ), "heartbeat text and timing are unchanged"
    record = metric_sink.records[0]
    assert record["BUILD_LIMITS_WAIT_SECONDS"] == "2.000", "heartbeat boundary"
    assert record["BUILD_LIMITS_RUNTIME_SECONDS"] == "2.000", "completion boundary"


class _DeadlineSeenError(Exception):
    """Stops an adapter run once its admission deadline has been read."""


@pytest.mark.parametrize("value", ["invalid", "5m", "inf", "nan", "-1", "0"])
def test_malformed_timeout_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, metric_sink: RecordingSink, value: str
) -> None:
    """An unusable unavailability timeout is ignored rather than failing the build."""
    monkeypatch.setenv("BUILD_LIMITS_UNAVAILABLE_SECS", value)
    deadlines: list[float] = []

    def capture(deadline: float, _summary: object) -> typ.NoReturn:
        """Record the admission deadline and stop before any connection.

        Raises
        ------
        _DeadlineSeenError
            Always, once the deadline has been recorded.
        """
        deadlines.append(deadline)
        raise _DeadlineSeenError

    monkeypatch.setattr(adapter, "AdmissionAttempt", capture)
    with pytest.raises(_DeadlineSeenError):
        adapter.run(
            adapter.Invocation(["rustc"], cacheable=True),
            retry=RetryWait(clock=Clock(now=lambda: 0.0)),
            metrics=Metrics(metric_sink, source="adapter"),
        )
    assert deadlines == [adapter.DEFAULT_UNAVAILABLE_SECONDS], (
        "the default deadline replaces the unusable value"
    )
