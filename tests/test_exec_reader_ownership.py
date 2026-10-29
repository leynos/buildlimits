"""Selector ownership of the close-on-exec launch diagnostic reader."""

import errno
import os
import selectors
from pathlib import Path

import pytest

from buildlimits.config import SupervisorConfig
from buildlimits.groups import Groups, Running
from buildlimits.launch import HeldChild, read_exec_outcome
from buildlimits.systemd import UserManager


def test_error_reader_unregisters_before_close_after_complete_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial data keeps its watch; EOF unregisters the still-open descriptor."""
    with selectors.DefaultSelector() as selector:
        groups = Groups(SupervisorConfig(), UserManager(), selector, lambda: 0.0)
        read, write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        child = HeldChild(0, None, read)
        groups.running["g1"] = Running("g1", 1, Path("/unused"), child)
        selector.register(read, selectors.EVENT_READ, ("errors", "g1"))
        original = selector.unregister
        inspected = []

        def unregister(fd: int) -> selectors.SelectorKey:
            """Require the selector to release ownership before closing the pipe."""
            os.fstat(fd)
            inspected.append(fd)
            return original(fd)

        monkeypatch.setattr(selector, "unregister", unregister)
        try:
            os.write(write, b"stage=exec")
            assert groups.exec_outcome("g1") is None, "partial data needs another event"
            assert selector.get_key(read).data == ("errors", "g1"), (
                "incomplete diagnostics retain their watch"
            )
            assert not inspected, "partial data does not release selector ownership"
            os.close(write)
            write = -1
            assert groups.exec_outcome("g1") == (1, "stage=exec"), (
                "EOF produces the complete launch failure"
            )
            assert inspected == [read], "the watch ends once while its pipe is open"
            assert child.errors is None, "the diagnostic descriptor closes at EOF"
        finally:
            if write >= 0:
                os.close(write)
            child.close()


def test_failed_selector_cleanup_still_closes_completed_reader() -> None:
    """An unregister fault propagates after diagnostic ownership has ended."""
    read, write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    child = HeldChild(0, None, read)
    os.close(write)

    def fail(_fd: int) -> object:
        """Inject a selector fault at the final ownership boundary."""
        msg = "injected unregister failure"
        raise OSError(msg)

    try:
        with pytest.raises(OSError, match="injected unregister"):
            read_exec_outcome(child, before_close=fail)
        assert child.errors is None, "failed cleanup cannot retain the completed reader"
        with pytest.raises(OSError, match="Bad file descriptor") as caught:
            os.fstat(read)
        assert caught.value.errno == errno.EBADF, "the completed reader is closed"
    finally:
        child.close()
