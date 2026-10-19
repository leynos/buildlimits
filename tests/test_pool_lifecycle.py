"""Model-based test of ``TokenPool``'s close protocol against reader orderings.

The pool coordinates a close flag, a reader thread that may be blocked inside
``read``, and the descriptors both release. This machine drives request, token
supply, close, repeated close and descriptor reuse in generated orders, and
checks after every step that each descriptor is released exactly once, that the
device is never released under a blocked read, that a stranger which reuses a
released number is left alone, and that the reader ends once nothing blocks it.
Blocked reads are made deterministic by learning when the reader has entered
``os.read``, so the model never guesses at thread timing.
"""

import contextlib
import os
import select
import tempfile
import threading
from pathlib import Path

import pytest
from descriptor_helpers import still_open
from hypothesis import settings
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)
from pool_helpers import announce_reads

from buildlimits.pool import Token, TokenPool


class PoolLifecycle(RuleBasedStateMachine):
    """Generate lifecycle operations on one pool over a private FIFO."""

    def __init__(self) -> None:
        """Start with no pool; ``start`` opens it once Hypothesis initializes."""
        super().__init__()
        # Hypothesis builds and tears down the machine itself, so the directory
        # cannot be scoped by a ``with`` block; ``teardown`` cleans it up.
        self.directory = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.pool: TokenPool | None = None
        self.entered_read = threading.Event()
        self.is_closed = False
        self.is_blocked = False
        self.stranger: int | None = None
        self.owned: list[int] = []

    @initialize()
    def start(self) -> None:
        """Open the pool and announce the reader's entry into ``os.read``."""
        fifo = Path(self.directory.name) / "guild"
        os.mkfifo(fifo)
        self.pool = TokenPool(fifo)
        device = self.opened.fd
        self.owned = [device, self.opened.wake_fd, self.opened._wake_write]
        self.patch = pytest.MonkeyPatch()
        announce_reads(self.patch, device, self.entered_read)

    @property
    def opened(self) -> TokenPool:
        """The pool, which ``start`` has opened before any rule runs.

        Returns
        -------
        TokenPool
            The pool under test.
        """
        assert self.pool is not None, "the pool was opened by start"
        return self.pool

    def _await_event(self) -> list[object]:
        """Wait for the reader's wake-up, then collect the pool's events."""
        ready, _, _ = select.select([self.opened.wake_fd], [], [], 5.0)
        assert ready, "the reader woke the loop"
        return list(self.opened.events())

    @precondition(lambda self: not self.is_closed and not self.is_blocked)
    @rule()
    def take_a_token(self) -> None:
        """Return a token, ask for it, and receive it unchanged."""
        self.opened.give_back(b"t")
        self.opened.request()
        assert self._await_event() == [Token(b"t")], "the returned byte comes back"

    @precondition(lambda self: not self.is_closed and not self.is_blocked)
    @rule()
    def block_the_reader(self) -> None:
        """Ask for a token that is not there, and wait until the read blocks."""
        self.entered_read.clear()
        self.opened.request()
        assert self.entered_read.wait(5.0), "the reader reached its blocking read"
        self.is_blocked = True

    @precondition(lambda self: self.is_blocked)
    @rule()
    def unblock_the_reader(self) -> None:
        """Supply the token a blocked read waits for."""
        # The device is still open here by invariant, so the write is safe.
        os.write(self.opened.fd, b"+")
        self.is_blocked = False
        if not self.is_closed:
            assert self._await_event() == [Token(b"+")], "the live pool reports it"
        else:
            self.opened._reader.join(5.0)
            assert not self.opened._reader.is_alive(), "a closed pool's reader ends"

    @rule()
    def close(self) -> None:
        """Close the pool, once or repeatedly."""
        self.opened.close()
        self.is_closed = True

    @precondition(lambda self: self.is_closed and not self.is_blocked)
    @rule()
    def reuse_the_released_device_number(self) -> None:
        """Put an unrelated descriptor on the number the device released."""
        device = self.owned[0]
        if self.stranger is not None or still_open([device]):
            return
        spare = os.open(os.devnull, os.O_RDONLY)
        if spare != device:
            os.dup2(spare, device)
            os.close(spare)
        self.stranger = device

    @invariant()
    def descriptors_are_released_exactly_once(self) -> None:
        """Check ownership of every descriptor against the model."""
        if self.pool is None or not self.is_closed:
            return
        wake = self.owned[1:]
        assert not still_open(wake), "the wake pipe is released at once"
        device = self.owned[0]
        if self.is_blocked:
            assert still_open([device]) == [device], "never closed under a read"
        if self.stranger is not None:
            assert still_open([self.stranger]) == [self.stranger], (
                "a repeated close leaves a reused number alone"
            )

    def teardown(self) -> None:
        """Unblock and close the pool, then return everything the run opened."""
        try:
            if self.pool is not None:
                if self.is_blocked:
                    with contextlib.suppress(OSError):
                        os.write(self.opened.fd, b"+")
                self.opened.close()
                self.opened._reader.join(5.0)
                assert not self.opened._reader.is_alive(), "the reader always ends"
                if self.stranger is None:
                    assert not still_open(self.owned), "nothing is left open"
        finally:
            if self.stranger is not None:
                os.close(self.stranger)
            if self.pool is not None:
                self.patch.undo()
            self.directory.cleanup()


TestPoolLifecycle = PoolLifecycle.TestCase
TestPoolLifecycle.settings = settings(
    max_examples=30,
    stateful_step_count=10,
    deadline=None,
    database=None,
)
