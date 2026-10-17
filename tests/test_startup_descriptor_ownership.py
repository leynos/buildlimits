"""Startup failures close lock and listener descriptors without relying on GC."""

import errno
import socket
from pathlib import Path

import pytest
from descriptor_helpers import isolated, open_fds

from buildlimits import requesters, startup


def _lock_failure(_report: int, path: str) -> object:
    """Inject a non-contention flock failure in a disposable child."""
    baseline = open_fds()
    with pytest.MonkeyPatch.context() as patch:

        def fail(_fd: int, _operation: int) -> None:
            """Fail after opening the lock descriptor."""
            raise OSError(errno.EIO, "injected lock failure")

        patch.setattr(startup.fcntl, "flock", fail)
        with pytest.raises(OSError, match="injected lock failure") as caught:
            startup.acquire_lock(Path(path))
        return {"errno": caught.value.errno, "baseline": open_fds() == baseline}


def test_lock_failure_reclaims_opened_descriptor(tmp_path: Path) -> None:
    """Unexpected lock errors preserve caller ownership and resource limits."""
    assert isolated(lambda report: _lock_failure(report, str(tmp_path / "lock"))) == {
        "errno": errno.EIO,
        "baseline": True,
    }, "failed locking restores the exact descriptor baseline"


def _listen_failure(_report: int, path: str) -> object:
    """Retain the allocated socket so cleanup cannot depend on object collection."""
    baseline = open_fds()
    created: list[socket.socket] = []
    original = socket.socket
    with pytest.MonkeyPatch.context() as patch:

        def allocate(family: int, kind: int) -> socket.socket:
            """Record the allocated listener independently of its failing caller."""
            listener = original(family, kind)
            created.append(listener)
            return listener

        def fail(_listener: socket.socket, _address: str) -> None:
            """Fail binding after the listening socket has been allocated."""
            raise OSError(errno.EADDRINUSE, "injected bind failure")

        patch.setattr(socket, "socket", allocate)
        patch.setattr(original, "bind", fail)
        with pytest.raises(OSError, match="injected bind failure") as caught:
            requesters.listen(Path(path))
        result = {
            "errno": caught.value.errno,
            "closed": created[0].fileno() == -1,
            "baseline": open_fds() == baseline,
        }
        for listener in created:
            listener.close()
        return result


def test_failed_listen_closes_even_a_retained_socket(tmp_path: Path) -> None:
    """A failed listener owns no descriptor even if another reference survives."""
    assert isolated(
        lambda report: _listen_failure(report, str(tmp_path / "listener.sock"))
    ) == {"errno": errno.EADDRINUSE, "closed": True, "baseline": True}, (
        "failed listener creation closes owned resources despite retained references"
    )
