"""Tests for the token pool's reader thread and device checks."""

import os
import select
import typing as typ

from buildlimits.pool import PoolFailure, Token, TokenPool

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.pool import PoolEvent


def _next_events(pool: TokenPool, timeout: float = 5.0) -> list[PoolEvent]:
    """Wait for the reader thread to wake the loop, then collect its events."""
    ready, _writable, _errored = select.select([pool.wake_fd], [], [], timeout)
    assert ready, "the reader thread woke the event loop"
    return pool.events()


def test_tokens_are_read_only_on_request(tmp_path: Path) -> None:
    """The reader takes one token per request and none unasked."""
    fifo = tmp_path / "guild"
    os.mkfifo(fifo)
    pool = TokenPool(fifo)
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


def test_a_returned_token_can_be_taken_again(tmp_path: Path) -> None:
    """A token written back is available to the next request unchanged."""
    fifo = tmp_path / "guild"
    os.mkfifo(fifo)
    pool = TokenPool(fifo)
    pool.give_back(b"z")
    pool.request()
    assert _next_events(pool) == [Token(b"z")], "the returned byte comes back"


def test_end_of_file_fails_the_pool(tmp_path: Path) -> None:
    """A device that ends is a failure the event loop hears about."""
    device = tmp_path / "guild"
    device.write_bytes(b"")
    pool = TokenPool(device)
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
    assert pool.is_same_device(), "the opened device is still in place"
    fifo.unlink()
    os.mkfifo(fifo)
    assert not pool.is_same_device(), "a new device at the same path differs"
    fifo.unlink()
    assert not pool.is_same_device(), "a missing device differs"


def test_the_handle_is_not_inherited(tmp_path: Path) -> None:
    """Compilers must not inherit the pool's handle, or its tokens outlive a crash."""
    fifo = tmp_path / "guild"
    os.mkfifo(fifo)
    pool = TokenPool(fifo)
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
