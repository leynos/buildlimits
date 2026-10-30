"""Bounded non-blocking process output for the supervisor loop."""

import contextlib
import dataclasses as dc
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell.
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

type ChildFailure = typ.Literal["timeout", "read_error", "cancelled"]

MAX_ACTIVE_CHILDREN = 4
MAX_OUTPUT_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024


@dc.dataclass(frozen=True, slots=True)
class ChildResult:
    """Terminal, bounded result from one helper process."""

    output: bytes
    returncode: int
    failure: ChildFailure | None


class NonBlockingChild:
    """Own one helper process and its non-blocking stdout pipe.

    Parameters
    ----------
    argv : Sequence[str]
        Fixed argument vector whose executable path is already absolute.
    env : Mapping[str, str]
        Complete environment passed to the helper process.
    deadline : float
        Absolute monotonic deadline supplied by the caller.

    Examples
    --------
    ``NonBlockingChild((sys.executable, "-c", "pass"), env, 10.0)`` starts
    one helper with no input and a non-blocking output pipe.
    """

    def __init__(
        self,
        argv: cabc.Sequence[str],
        env: cabc.Mapping[str, str],
        deadline: float,
    ) -> None:
        """Start a shell-free helper with bounded output ownership.

        Raises
        ------
        ValueError
            If the argument vector is empty or its executable is not absolute.
        """
        if not argv or not Path(argv[0]).is_absolute():
            msg = "helper executable must be an absolute path"
            raise ValueError(msg)
        self.deadline = deadline
        self._output = bytearray()
        self._failure: ChildFailure | None = None
        self._result: ChildResult | None = None
        self._reaped = False
        self._stdout_eof = False
        # ChildReaper owns this process across ticks; a context would end it
        # when this constructor returns.
        # pylint: disable=consider-using-with
        self._process = subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv and no shell.
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(env),
            bufsize=0,
            close_fds=True,
        )
        # pylint: enable=consider-using-with
        self._stream = self._process.stdout
        if self._stream is None:
            self._fail("read_error")
        else:
            try:
                os.set_blocking(self._stream.fileno(), False)
            except OSError:
                self._fail("read_error")

    @property
    def result(self) -> ChildResult | None:
        """Terminal process result, or None while it is active.

        Returns
        -------
        ChildResult or None
            Captured output and exit status after the process is reaped.
        """
        return self._result

    @property
    def stdout_open(self) -> bool:
        """Report whether the supervisor still owns the output pipe.

        Returns
        -------
        bool
            True only while the pipe is open.
        """
        return self._stream is not None

    @property
    def reaped(self) -> bool:
        """Report whether ``Popen.poll`` collected the process status.

        Returns
        -------
        bool
            True after the operating system no longer reports a child.
        """
        return self._reaped

    def poll(self, now: float) -> None:
        """Advance the child without waiting for the process or its pipe.

        Parameters
        ----------
        now : float
            Current monotonic time supplied by the supervisor loop.

        Examples
        --------
        Polling before a deadline leaves a live child pending.
        """
        if self._result is not None:
            return
        self._expire_if_late(now)
        if self._failure is None:
            self._read_available()
        returncode = self._poll_status()
        if returncode is not None:
            self._finish(returncode)

    def _expire_if_late(self, now: float) -> None:
        """Record a timeout once the caller-supplied deadline has passed."""
        if self._failure is None and now >= self.deadline:
            self._fail("timeout")

    def _poll_status(self) -> int | None:
        """Read the exit status without waiting, recording an OS failure.

        Returns
        -------
        int or None
            The exit status once the child has ended, otherwise ``None``.
        """
        try:
            return self._process.poll()
        except OSError:
            self._fail("read_error")
            return None

    def _finish(self, returncode: int) -> None:
        """Record the reaped status once the child can no longer produce output."""
        self._reaped = True
        if self._failure is not None or self._stdout_eof:
            self._close_stdout()
            self._result = ChildResult(bytes(self._output), returncode, self._failure)

    def cancel(self) -> None:
        """Close the pipe and kill an unfinished helper without waiting.

        Examples
        --------
        A cancelled child remains owned by its reaper until a later poll reaps it.
        """
        if self._result is None:
            self._fail("cancelled")

    def _read_available(self) -> None:
        """Drain ready bytes without exceeding the fixed output bound."""
        stream = self._stream
        if stream is None:
            return
        while True:
            available = MAX_OUTPUT_BYTES - len(self._output)
            try:
                block = os.read(stream.fileno(), min(_READ_CHUNK_BYTES, available + 1))
            except BlockingIOError:
                return
            except OSError:
                self._fail("read_error")
                return
            if not block:
                self._stdout_eof = True
                self._close_stdout()
                return
            if len(block) > available:
                self._fail("read_error")
                return
            self._output.extend(block)

    def _fail(self, reason: ChildFailure) -> None:
        """Stop reading, close the pipe and signal only this owned process."""
        if self._failure is not None:
            return
        self._failure = reason
        self._stdout_eof = True
        self._close_stdout()
        with contextlib.suppress(OSError):
            self._process.kill()

    def _close_stdout(self) -> None:
        """Close the pipe exactly once while leaving process reaping to poll."""
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()


class ChildReaper:
    """Poll and reap no more than four owned helper processes per tick.

    Examples
    --------
    A full reaper returns None from ``start`` without spawning a fifth child.
    """

    def __init__(self) -> None:
        """Create an empty reaper with the fixed process bound."""
        self._children: list[NonBlockingChild] = []

    @property
    def active_count(self) -> int:
        """Number of children still owned by this reaper."""
        return len(self._children)

    def start(
        self,
        argv: cabc.Sequence[str],
        env: cabc.Mapping[str, str],
        *,
        deadline: float,
    ) -> NonBlockingChild | None:
        """Start one child unless every bounded reaper slot is occupied.

        Returns
        -------
        NonBlockingChild or None
            The owned child, or None when the reaper has no free slot.
        """
        if len(self._children) >= MAX_ACTIVE_CHILDREN:
            return None
        child = NonBlockingChild(argv, env, deadline)
        self._children.append(child)
        return child

    def poll(self, now: float) -> None:
        """Poll every owned process once and release reaped children.

        Parameters
        ----------
        now : float
            Current monotonic time supplied by the caller.
        """
        for child in tuple(self._children):
            child.poll(now)
        self._children = [child for child in self._children if child.result is None]

    def cancel(self, child: NonBlockingChild) -> None:
        """Request termination while retaining ownership until a later poll.

        Parameters
        ----------
        child : NonBlockingChild
            Child owned by this reaper.
        """
        if child in self._children:
            child.cancel()
