"""The guildmaster token pool, read without blocking the supervisor.

The supervisor is the pool's only reader. It holds exactly one handle on the
device, opened with ``O_CLOEXEC`` so that no compiler inherits it: guildmaster
keys its accounting on the process that opened the device and returns that
process's tokens only when the last copy of the handle closes, so an inherited
copy would keep the supervisor's tokens alive after a crash.

Whether guildmaster's CUSE device supports ``poll`` is not something the
supervisor relies on. A dedicated thread performs blocking one-byte reads
while a request is waiting, and reports each token, or the error that ends the
pool's health, through a pipe that the event loop watches. The event loop
therefore stays responsive to cancellations and compiler exits while the pool
is empty.
"""

import contextlib
import dataclasses as dc
import os
import queue
import threading
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


@dc.dataclass(frozen=True, slots=True)
class Token:
    """A token read from the pool.

    Attributes
    ----------
    value : bytes
        The single byte read, returned unchanged.
    """

    value: bytes


@dc.dataclass(frozen=True, slots=True)
class PoolFailure:
    """The pool can no longer be used.

    Attributes
    ----------
    reason : str
        A human-readable description.
    """

    reason: str


type PoolEvent = Token | PoolFailure


class TokenPool:
    """One close-on-exec handle on a jobserver-style token device.

    Parameters
    ----------
    path : Path
        The device, normally ``/dev/guild``. Tests use a FIFO primed with
        tokens, which behaves the same for a single reader.

    Attributes
    ----------
    request : Callable[[], None]
        Asks the reader thread for one more token; each call yields one
        :class:`Token` or a :class:`PoolFailure` through :meth:`events`.

    Raises
    ------
    OSError
        If the device cannot be opened.
    """

    def __init__(self, path: Path) -> None:
        """Open the device and start the reader thread."""
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        stat = os.fstat(self.fd)
        self.identity = (stat.st_dev, stat.st_ino, stat.st_rdev)
        self._events: queue.SimpleQueue[PoolEvent] = queue.SimpleQueue()
        self._wake_read, self._wake_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        self._demand = threading.Semaphore(0)
        # Each release asks the reader thread for one more token.
        self.request: cabc.Callable[[], None] = self._demand.release
        self._reader = threading.Thread(target=self._read_forever, daemon=True)
        self._reader.start()

    @property
    def wake_fd(self) -> int:
        """The descriptor that becomes readable when an event is ready.

        Returns
        -------
        int
            The pipe's read end.
        """
        return self._wake_read

    def events(self) -> list[PoolEvent]:
        """Collect the events the reader thread has produced.

        Returns
        -------
        list[PoolEvent]
            Tokens and failures, in the order they occurred.
        """
        with contextlib.suppress(BlockingIOError):
            while os.read(self._wake_read, 4096):
                pass
        collected: list[PoolEvent] = []
        while True:
            try:
                collected.append(self._events.get_nowait())
            except queue.Empty:
                return collected

    def give_back(self, token: bytes) -> None:
        """Return a token to the pool.

        Parameters
        ----------
        token : bytes
            The byte originally read.

        Raises
        ------
        OSError
            If the device rejects the write; the pool is then unhealthy.
        """
        written = os.write(self.fd, token)
        if written != len(token):
            msg = "short write returning a token"
            raise OSError(msg)

    def is_same_device(self) -> bool:
        """Check that the device path still names the opened device.

        A guildmaster restart replaces ``/dev/guild`` with a new, full pool;
        the old handle then belongs to a pool that no longer exists.

        Returns
        -------
        bool
            Whether the path still resolves to the device held open.
        """
        try:
            stat = self.path.stat()
        except OSError:
            return False
        return (stat.st_dev, stat.st_ino, stat.st_rdev) == self.identity

    def _read_forever(self) -> None:
        """Serve token requests until the pool fails."""
        while True:
            # The semaphore counts requested tokens; it guards no region, so a
            # ``with`` block would wrongly release it.
            self._demand.acquire()  # pylint: disable=consider-using-with
            try:
                data = os.read(self.fd, 1)
            except OSError as error:
                self._emit(PoolFailure(f"reading a token failed: {error}"))
                return
            if not data:
                self._emit(PoolFailure("the token device reported end of file"))
                return
            self._emit(Token(data))

    def _emit(self, event: PoolEvent) -> None:
        """Queue an event and wake the event loop."""
        self._events.put(event)
        # A full pipe means the loop is already awake.
        with contextlib.suppress(BlockingIOError):
            os.write(self._wake_write, b"!")
