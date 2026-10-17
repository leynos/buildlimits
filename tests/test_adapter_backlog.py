"""A full Unix backlog cannot bypass the adapter's admission retry deadline."""

import contextlib
import signal
import socket
import time
import typing as typ

from descriptor_helpers import isolated, open_fds

from buildlimits import adapter
from buildlimits.heartbeat import Heartbeat

if typ.TYPE_CHECKING:
    from pathlib import Path


class _GuardExpiredError(Exception):
    """A disposable-child guard detected an unbounded connection attempt."""


def _fill_backlog(listener: socket.socket, owned: contextlib.ExitStack) -> None:
    """Retain nonblocking connections until the kernel refuses another one."""
    for _attempt in range(8):
        client = owned.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        client.setblocking(False)
        try:
            client.connect(listener.getsockname())
        except BlockingIOError:
            return
    msg = "the test listener backlog did not saturate"
    raise AssertionError(msg)


def _blocked(_report: int, path: Path) -> object:
    """Guard one real blocking connect inside the isolated low-limit child."""
    baseline = open_fds()
    result: dict[str, object]
    with contextlib.ExitStack() as owned:
        listener = owned.enter_context(
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        )
        listener.bind(str(path))
        listener.listen(1)
        _fill_backlog(listener, owned)
        heartbeat = Heartbeat("backlog", 600.0)
        deadline = time.monotonic() + 0.05

        def guard(_number: int, _frame: object) -> typ.NoReturn:
            """Bound the disposable experiment if the socket ignores its deadline."""
            raise _GuardExpiredError

        old_handler = signal.signal(signal.SIGALRM, guard)
        signal.setitimer(signal.ITIMER_REAL, 0.5)
        try:
            connection = adapter.connect(path, deadline, heartbeat)
        except adapter.AdapterError:
            result = {"outcome": "bounded-refusal"}
        except _GuardExpiredError:
            result = {"outcome": "guard-expired"}
        else:
            connection.close()
            result = {"outcome": "unexpected-connection"}
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
    result["baseline"] = open_fds() == baseline
    return result


def test_full_backlog_preserves_the_connection_deadline(tmp_path: Path) -> None:
    """Queued connection pressure ends with a bounded infrastructure refusal."""
    assert isolated(lambda report: _blocked(report, tmp_path / "backlog.sock")) == {
        "outcome": "bounded-refusal",
        "baseline": True,
    }, "full backlog respects its deadline and restores descriptor ownership"
