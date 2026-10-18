"""Native metric sockets recover from resource and close failures without leaks."""

import errno
import functools
import os
import resource
import socket
import typing as typ

import pytest
from descriptor_helpers import isolated, open_fds
from journal_fixtures import journal_receiver

from buildlimits import journal_native
from buildlimits.journal_native import MAX_DATAGRAM_BYTES, JournalSink
from buildlimits.metrics import Metrics

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


class _CloseFailureSocket(socket.socket):
    """Fail the first send and report a close error after closing the actual fd."""

    def __init__(self) -> None:
        """Create one real native socket and its invocation-local send counter."""
        super().__init__(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sends = 0

    def send(self, data: object, flags: int = 0) -> int:
        """Reject the first datagram, then retain real socket error semantics.

        Returns
        -------
        int
            The bytes sent by the underlying socket.

        Raises
        ------
        OSError
            On the controlled first-send failure or an underlying socket error.
        """
        self.sends += 1
        if self.sends == 1:
            raise OSError(errno.EMFILE, "controlled send failure")
        return super().send(typ.cast("bytes", data), flags)

    def close(self) -> None:
        """Invalidate the actual descriptor before surfacing the close error.

        Raises
        ------
        OSError
            After the actual socket descriptor has already been closed.
        """
        super().close()
        raise OSError(errno.EIO, "controlled close failure")


def _first_then_fresh(
    first: socket.socket,
) -> tuple[cabc.Callable[[int, int], socket.socket], list[socket.socket]]:
    """Hand out a controlled socket once, then fresh native sockets.

    The real constructor is captured here, before a test patches the module's
    ``socket.socket`` with the returned factory.

    Returns
    -------
    tuple[Callable[[int, int], socket.socket], list[socket.socket]]
        The factory and the list of every socket it has handed out.
    """
    constructor = socket.socket
    created: list[socket.socket] = []

    def make_socket(family: int, kind: int) -> socket.socket:
        """Return the controlled socket once, then create fresh real sockets.

        Returns
        -------
        socket.socket
            The controlled first socket or a fresh native replacement.
        """
        conn = first if not created else constructor(family, kind)
        created.append(conn)
        return conn

    return make_socket, created


def test_close_error_permits_immediate_fresh_native_emission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed close cannot leave a stale socket cached for the next metric."""
    path = tmp_path / "journal"
    with journal_receiver(path, timeout=0.05) as receiver:
        failed = _CloseFailureSocket()
        make_socket, created = _first_then_fresh(failed)
        monkeypatch.setattr(journal_native.socket, "socket", make_socket)
        warnings: list[str] = []
        sink = JournalSink(path)
        metrics = Metrics(sink, warn=warnings.append)
        try:
            metrics.emit("request_queued", queued=1)
            assert failed.fileno() == -1, "CPython invalidated the failed socket fd"
            metrics.emit("request_queued", queued=2)
            payload = receiver.recv(MAX_DATAGRAM_BYTES)
            assert b"BUILD_LIMITS_QUEUED=2\n" in payload, "the next record is delivered"
            assert len(created) == 2, "recovery allocates a fresh socket immediately"
            assert len(warnings) == 1, "telemetry failure warns at most once"
        finally:
            for conn in created:
                socket.SocketType.close(conn)


@pytest.mark.parametrize(
    "number", [errno.EMFILE, errno.ENFILE], ids=["emfile", "enfile"]
)
def test_socket_allocation_failure_recovers_without_leaking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number: int
) -> None:
    """A constructor resource failure drops one record and retries lazily."""
    constructor = socket.socket
    path = tmp_path / "journal"
    with journal_receiver(path, timeout=0.05) as receiver:
        attempts = []
        created: list[socket.socket] = []

        def make_socket(family: int, kind: int) -> socket.socket:
            """Fail allocation once, then retain the actual recovery socket.

            Returns
            -------
            socket.socket
                The native datagram socket after pressure clears.

            Raises
            ------
            OSError
                On the controlled first allocation failure.
            """
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError(number, "controlled descriptor pressure")
            conn = constructor(family, kind)
            created.append(conn)
            return conn

        monkeypatch.setattr(journal_native.socket, "socket", make_socket)
        warnings: list[str] = []
        sink = JournalSink(path)
        metrics = Metrics(sink, warn=warnings.append)
        try:
            metrics.emit("request_queued", queued=1)
            assert not created, "failed construction allocated no owned socket"
            metrics.emit("request_queued", queued=2)
            metrics.emit("request_queued", queued=3)
            assert b"BUILD_LIMITS_QUEUED=2\n" in receiver.recv(MAX_DATAGRAM_BYTES), (
                "the first recovery record is delivered"
            )
            assert b"BUILD_LIMITS_QUEUED=3\n" in receiver.recv(MAX_DATAGRAM_BYTES), (
                "the cached socket delivers the following record"
            )
            assert len(created) == 1, "only one native socket remains live"
            assert len(attempts) == 2, "no eager allocation retry occurs"
            assert len(warnings) == 1, "failure remains isolated and warns once"
        finally:
            sink.close()
        assert created[0].fileno() == -1, "owned recovery socket is closed"


class _TransportFailureSocket(socket.socket):
    """Expose controlled connect or send pressure with a real owned descriptor."""

    def __init__(self, phase: str) -> None:
        """Create one native socket and remember the controlled failure phase."""
        super().__init__(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.phase = phase
        self.closes = 0

    def connect(self, address: object) -> None:
        """Fail only the named connect boundary before sending any record.

        Raises
        ------
        OSError
            On controlled connect pressure or an underlying socket error.
        """
        if self.phase == "connect":
            raise OSError(errno.EMFILE, "controlled connect pressure")
        super().connect(typ.cast("str", address))

    def send(self, data: object, flags: int = 0) -> int:
        """Fail the named send boundary after a successful native connection.

        Returns
        -------
        int
            The actual transmitted byte count when no fault is configured.

        Raises
        ------
        OSError
            On controlled send pressure or an underlying socket error.
        """
        if self.phase == "send":
            raise OSError(errno.ENFILE, "controlled send pressure")
        return super().send(typ.cast("bytes", data), flags)

    def close(self) -> None:
        """Record one ownership release and close the actual descriptor."""
        self.closes += 1
        super().close()


@pytest.mark.parametrize("phase", ["connect", "send"])
def test_transport_failure_closes_once_and_reopens_one_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """Connection and send faults release ownership before the next emission."""
    path = tmp_path / "journal"
    with journal_receiver(path, timeout=0.05) as receiver:
        failed = _TransportFailureSocket(phase)
        make_socket, created = _first_then_fresh(failed)
        monkeypatch.setattr(journal_native.socket, "socket", make_socket)
        sink = JournalSink(path)
        metrics = Metrics(sink)
        try:
            metrics.emit("request_queued", queued=1)
            assert failed.fileno() == -1, "the failed descriptor is released"
            assert failed.closes == 1, "failure closes the owned socket once"
            metrics.emit("request_queued", queued=2)
            metrics.emit("request_queued", queued=3)
            receiver.recv(MAX_DATAGRAM_BYTES)
            receiver.recv(MAX_DATAGRAM_BYTES)
            assert len(created) == 2, "successful emission reuses one replacement"
            assert sum(conn.fileno() >= 0 for conn in created) == 1, (
                "only the replacement native socket remains live"
            )
            assert failed.closes == 1, "recovery never closes the stale object again"
        finally:
            sink.close()
        assert all(conn.fileno() == -1 for conn in created), "all owned sockets close"


def test_repeated_close_does_not_touch_a_reused_descriptor(tmp_path: Path) -> None:
    """Reusing a released descriptor number does not transfer socket ownership."""
    path = tmp_path / "journal"
    with journal_receiver(path):
        sink = JournalSink(path)
        sink.emit({"BUILD_LIMITS_EVENT": "request_queued"})
        owned = sink._socket
        assert owned is not None, "the sink allocated its native socket"
        number = owned.fileno()
        sink.close()
        assert owned.fileno() == -1, "CPython invalidates the socket object's fd"
        foreign = os.open(os.devnull, os.O_RDONLY)
        try:
            assert foreign == number, "the next descriptor reuses the released number"
            sink.close()
            sink.close()
            os.fstat(foreign)
            assert owned.fileno() == -1, "the old object retains no numeric ownership"
        finally:
            os.close(foreign)


def _recover_at_descriptor_limit(_report: int, *, path: Path) -> dict[str, object]:
    """Exhaust only the isolated child's descriptors, then free one for emission.

    Returns
    -------
    dict[str, object]
        Real allocation errno, recovery payload and descriptor-baseline evidence.
    """
    baseline = open_fds()
    fillers: list[int] = []
    number = None
    payload = b""
    with journal_receiver(path) as receiver:
        sink = JournalSink(path)
        metrics = Metrics(sink)
        try:
            while True:
                try:
                    fillers.append(os.open(os.devnull, os.O_RDONLY))
                except OSError as error:
                    number = error.errno
                    break
            metrics.emit("request_queued", queued=1)
            dropped = metrics.dropped_records
            os.close(fillers.pop())
            metrics.emit("request_queued", queued=2)
            payload = receiver.recv(MAX_DATAGRAM_BYTES)
        finally:
            sink.close()
            for fd in fillers:
                os.close(fd)
    return {
        "errno": number,
        "dropped": dropped,
        "payload": payload.decode("ascii"),
        "soft": resource.getrlimit(resource.RLIMIT_NOFILE)[0],
        "baseline": open_fds() == baseline,
    }


def test_real_emfile_recovers_after_freeing_one_child_descriptor(
    tmp_path: Path,
) -> None:
    """A 64-fd child demonstrates real EMFILE, recovery and complete cleanup."""
    result = isolated(
        functools.partial(_recover_at_descriptor_limit, path=tmp_path / "journal")
    )
    assert isinstance(result, dict), "the isolated child reports structured evidence"
    assert result["soft"] == 64, "only the child receives the constrained soft limit"
    assert result["errno"] == errno.EMFILE, "real descriptor exhaustion produced EMFILE"
    assert result["dropped"] == 1, "the exhausted journal allocation dropped one record"
    assert "BUILD_LIMITS_QUEUED=2\n" in result["payload"], (
        "freeing one descriptor permits real native metric delivery"
    )
    assert result["baseline"], "all child-owned descriptors return to baseline"
