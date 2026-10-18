"""Tests for the token pool's reader thread and device checks."""

import contextlib
import os
import select
import threading
import typing as typ

import pytest
from descriptor_helpers import still_open

from buildlimits import pool as pool_module
from buildlimits.pool import PoolFailure, Token, TokenPool

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.pool import PoolEvent


@pytest.fixture
def make_pool(owned: contextlib.ExitStack) -> cabc.Callable[[Path], TokenPool]:
    """Provide a constructor whose pools are closed when the test ends.

    Returns
    -------
    Callable[[Path], TokenPool]
        Opens a pool on a device and registers its close for teardown.
    """

    def build(device: Path) -> TokenPool:
        """Open a pool on ``device`` and own its close."""
        return owned.enter_context(contextlib.closing(TokenPool(device)))

    return build


def _next_events(pool: TokenPool, timeout: float = 5.0) -> list[PoolEvent]:
    """Wait for the reader thread to wake the loop, then collect its events."""
    ready, _writable, _errored = select.select([pool.wake_fd], [], [], timeout)
    assert ready, "the reader thread woke the event loop"
    return pool.events()


def test_tokens_are_read_only_on_request(
    tmp_path: Path, make_pool: cabc.Callable[[Path], TokenPool]
) -> None:
    """The reader takes one token per request and none unasked."""
    fifo = tmp_path / "guild"
    os.mkfifo(fifo)
    pool = make_pool(fifo)
    writer = os.open(fifo, os.O_WRONLY)
    try:
        os.write(writer, b"ab")
        pool.request()
        assert _next_events(pool) == [Token(b"a")], "one request takes one token"
        assert not select.select([pool.wake_fd], [], [], 0.2)[0], (
            "no token is taken without a request"
        )
        pool.request()
        assert _next_events(pool) == [Token(b"b")], "the next request takes the next"
    finally:
        os.close(writer)


def test_a_returned_token_can_be_taken_again(
    tmp_path: Path, make_pool: cabc.Callable[[Path], TokenPool]
) -> None:
    """A token written back is available to the next request unchanged."""
    fifo = tmp_path / "guild"
    os.mkfifo(fifo)
    pool = make_pool(fifo)
    pool.give_back(b"z")
    pool.request()
    assert _next_events(pool) == [Token(b"z")], "the returned byte comes back"


def test_end_of_file_fails_the_pool(
    tmp_path: Path, make_pool: cabc.Callable[[Path], TokenPool]
) -> None:
    """A device that ends is a failure the event loop hears about."""
    device = tmp_path / "guild"
    device.write_bytes(b"")
    pool = make_pool(device)
    pool.request()
    events = _next_events(pool)
    assert len(events) == 1, events
    assert isinstance(events[0], PoolFailure), events
    assert "end of file" in events[0].reason, events[0].reason


def test_a_replaced_device_is_detected(tmp_path: Path) -> None:
    """A restart that recreates the device path is noticed."""
    fifo = tmp_path / "guild"
    os.mkfifo(fifo)
    pool = TokenPool(fifo)
    try:
        assert pool.is_same_device(), "the opened device is still in place"
        fifo.unlink()
        os.mkfifo(fifo)
        assert not pool.is_same_device(), "a new device at the same path differs"
        fifo.unlink()
        assert not pool.is_same_device(), "a missing device differs"
    finally:
        pool.close()


def test_the_handle_is_not_inherited(
    tmp_path: Path, make_pool: cabc.Callable[[Path], TokenPool]
) -> None:
    """Compilers must not inherit the pool's handle, or its tokens outlive a crash."""
    fifo = tmp_path / "guild"
    os.mkfifo(fifo)
    pool = make_pool(fifo)
    assert not os.get_inheritable(pool.fd), "the handle is close-on-exec"


def test_pool_read_failure_is_one_dispatch_metric(metrics, metric_sink) -> None:
    """Pool event dispatch labels read failures without their raw error text."""
    import typing as typ

    from buildlimits.config import SupervisorConfig
    from buildlimits.pool import PoolFailure
    from buildlimits.supervisor import Supervisor

    class FailedPool:
        """Expose one read failure through the real supervisor boundary."""

        def events(self) -> list[PoolFailure]:
            """Return the read failure."""
            return [PoolFailure("secret device error")]

    supervisor = Supervisor(SupervisorConfig(), metrics=metrics)
    try:
        supervisor.pool = typ.cast("TokenPool", FailedPool())
        supervisor._pool_events()
    finally:
        supervisor.selector.close()
    failures = metric_sink.of_event("pool_failure")
    assert len(failures) == 1, "one pool failure observation"
    assert failures[0]["BUILD_LIMITS_OPERATION"] == "read", "operation bounded"
    assert "secret" not in str(metric_sink.records), "raw reason excluded"


def _pool_descriptors(pool: TokenPool) -> list[int]:
    """List every descriptor the pool owns, read before it is closed."""
    return [pool.fd, pool.wake_fd, pool._wake_write]


@pytest.mark.parametrize("has_failed", [False, True], ids=["idle", "failed"])
def test_close_releases_every_descriptor_the_pool_owns(
    tmp_path: Path, has_failed: bool
) -> None:
    """A closed pool leaves neither its device nor its wake pipe open.

    The failed case is a reader that already ended on end of file; it is closed
    all the same.
    """
    device = tmp_path / "guild"
    if has_failed:
        device.write_bytes(b"")
    else:
        os.mkfifo(device)
    pool = TokenPool(device)
    owned_fds = _pool_descriptors(pool)
    assert still_open(owned_fds) == owned_fds, "the pool holds all three while open"
    if has_failed:
        pool.request()
        assert isinstance(_next_events(pool)[0], PoolFailure), "the reader failed"
        pool._reader.join(5.0)
    pool.close()
    assert not still_open(owned_fds), "close returns the device and both pipe ends"
    assert not pool._reader.is_alive(), "close ends the reader thread"


def test_close_is_idempotent_and_survives_descriptor_reuse(tmp_path: Path) -> None:
    """A second close must not close a descriptor that now belongs to someone else."""
    fifo = tmp_path / "guild"
    os.mkfifo(fifo)
    pool = TokenPool(fifo)
    device = pool.fd
    pool.close()
    # Pin an unrelated descriptor onto the released number, which is what the
    # kernel's lowest-free rule does to a real process.
    spare = os.open(os.devnull, os.O_RDONLY)
    if spare != device:
        os.dup2(spare, device)
        os.close(spare)
    try:
        pool.close()
        assert still_open([device]) == [device], "the repeat close left it alone"
    finally:
        os.close(device)


def test_close_leaves_a_blocked_read_to_close_the_device_when_it_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The device is never closed under a read; the reader closes it afterwards.

    Closing the descriptor while the reader thread is inside ``read`` would let
    the number be reused before the thread's next read, so ``close`` must not do
    it. The wake pipe has no such reader and closes at once.
    """
    fifo = tmp_path / "guild"
    os.mkfifo(fifo)
    pool = TokenPool(fifo)
    device, wake_read, wake_write = _pool_descriptors(pool)
    inside_read = threading.Event()
    real_read = os.read

    def announcing_read(fd: int, length: int) -> bytes:
        """Tell the test the reader is past its closing check, then really read."""
        if fd == device:
            inside_read.set()
        return real_read(fd, length)

    monkeypatch.setattr(pool_module.os, "read", announcing_read)
    pool.request()  # the FIFO is empty, so the reader blocks inside read
    assert inside_read.wait(5.0), "the reader reached its blocking read"
    pool.close()
    assert not still_open([wake_read, wake_write]), "the wake pipe closes at once"
    assert still_open([device]) == [device], "the device stays open under the read"
    writer = os.open(fifo, os.O_WRONLY)
    try:
        os.write(writer, b"+")
        pool._reader.join(5.0)
    finally:
        os.close(writer)
    assert not pool._reader.is_alive(), "the returned read lets the reader finish"
    assert not still_open([device]), "the reader closed the device it was using"
