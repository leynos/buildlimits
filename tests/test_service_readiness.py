"""The notifying service becomes ready only through its owned notification socket."""

import socket
from pathlib import Path

import pytest

from buildlimits.service_readiness import notify_ready, notify_status


def test_readiness_without_notification_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """A foreground supervisor needs no unrelated systemd notification endpoint."""
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notify_ready()


def test_readiness_sends_bounded_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readiness boundary sends the protocol marker to the fixture endpoint."""
    address = tmp_path / "notify.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(str(address))
        receiver.settimeout(2)
        monkeypatch.setenv("NOTIFY_SOCKET", str(address))
        notify_ready()
        message = receiver.recv(4096)
    assert message.startswith(b"READY=1\nSTATUS="), "systemd receives readiness"
    assert len(message) < 256, "readiness contains bounded state without credentials"


def test_unreachable_notification_socket_fails_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A notifying service cannot silently skip its manager readiness barrier."""
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "missing.sock"))
    with pytest.raises(FileNotFoundError):
        notify_ready()


def test_stopping_status_reaches_the_manager_without_marking_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advisory status line carries no READY marker for a stopping service."""
    address = tmp_path / "notify.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(str(address))
        receiver.settimeout(2)
        monkeypatch.setenv("NOTIFY_SOCKET", str(address))
        notify_status("Stopping: waiting for running compiler groups to finish")
        message = receiver.recv(4096)
    assert (
        message == b"STATUS=Stopping: waiting for running compiler groups to finish"
    ), "only the status line is sent"


def test_an_unreachable_manager_does_not_stop_a_stopping_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Advisory status is best effort: draining must go on without the manager."""
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "missing.sock"))
    notify_status("Stopping")
