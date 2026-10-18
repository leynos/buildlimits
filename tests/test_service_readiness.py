"""The notifying service becomes ready only through its owned notification socket."""

import socket
from pathlib import Path

import pytest

from buildlimits.service_readiness import notify_ready


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
