"""The service closes its native metric socket independently of supervisor exits."""

import contextlib
import socket
import typing as typ
import unittest.mock as mock  # ruff: ignore[manual-from-import] - module alias required by .rules/python-typing.md.
from pathlib import PurePosixPath

import pytest
from descriptor_helpers import open_fds

from buildlimits import cgroups, service
from buildlimits.journal_native import MAX_DATAGRAM_BYTES, JournalSink
from buildlimits.supervisor import Supervisor, SupervisorTelemetry

if typ.TYPE_CHECKING:
    from pathlib import Path


def _controlled_supervisor(phase: str, error: OSError) -> mock.Mock:
    """Configure the named lifecycle failure independently of metric ownership.

    Returns
    -------
    Mock
        Supervisor with the original serving status or a controlled fault.
    """
    # A name list keeps attribute drift detectable without ``inspect.signature``,
    # which on Python 3.14 evaluates the constructor's annotations and cannot
    # resolve names imported only for type checking.
    supervisor = mock.Mock(spec=dir(Supervisor))
    supervisor.serve.return_value = 17
    if phase == "start":
        supervisor.start.side_effect = error
    if phase == "serve":
        supervisor.serve.side_effect = error
    return supervisor


@pytest.mark.parametrize("phase", ["normal", "construct", "start", "serve", "close"])
def test_service_native_metrics_ownership_on_every_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """Construction, start, serving and close faults cannot retain the journal fd."""
    config_path = tmp_path / "supervisor.toml"
    config_path.write_text(f'lock_path = "{tmp_path / "lock"}"\n', encoding="utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        path = tmp_path / "journal"
        receiver.bind(str(path))
        receiver.settimeout(0.05)
        baseline = open_fds()
        sink = JournalSink(path)
        error = OSError("controlled service failure")
        supervisor = _controlled_supervisor(phase, error)

        def factory(_settings: object, *, telemetry: SupervisorTelemetry) -> mock.Mock:
            """Allocate a real metric socket before the controlled supervisor result.

            Returns
            -------
            Mock
                The supervisor with its named lifecycle outcome.

            Notes
            -----
            The constructor-failure case raises the supplied controlled error.
            """
            if telemetry.metrics is None:
                msg = "service must provide metrics to the supervisor"
                raise AssertionError(msg)
            telemetry.metrics.emit("supervisor_started")
            if phase == "construct":
                raise error
            return supervisor

        close = sink.close

        def failing_close() -> None:
            """Close the actual owned socket before surfacing a metric-only error.

            Raises
            ------
            OSError
                After releasing the owned metric descriptor.
            """
            close()
            msg = "private metric close failure"
            raise OSError(msg)

        monkeypatch.setattr(service, "Supervisor", factory)
        monkeypatch.setattr(service, "JournalSink", lambda **_kwargs: sink)
        monkeypatch.setattr(cgroups, "own_path", lambda: PurePosixPath("/service"))
        monkeypatch.setattr(service.signal, "signal", lambda *_args: None)
        if phase == "close":
            monkeypatch.setattr(sink, "close", failing_close)
        expected = (
            pytest.raises(OSError, match="controlled service failure")
            if phase in {"construct", "serve"}
            else contextlib.nullcontext()
        )
        status = None
        try:
            with expected:
                status = service.main(["--config", str(config_path)])
            if phase not in {"construct", "serve"}:
                assert status == (1 if phase == "start" else 17), (
                    "metrics closure does not alter supervisor status"
                )
            assert open_fds() == baseline, "journal and singleton-lock fds are released"
            assert b"BUILD_LIMITS_EVENT=supervisor_started\n" in receiver.recv(
                MAX_DATAGRAM_BYTES
            ), "service composition passes the native emitter to the supervisor"
        finally:
            close()
