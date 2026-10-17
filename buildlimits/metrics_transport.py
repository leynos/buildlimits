"""Bounded streaming transport for the development-host journal query."""

import os
import selectors
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed journal command, never a shell.
import time
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

MAX_QUERY_BYTES = 64 * 1024 * 1024
QUERY_TIMEOUT_SECONDS = 30.0


class JournalOutputError(subprocess.SubprocessError):
    """Journal output exceeded its byte budget or was not UTF-8."""


def run(
    argv: cabc.Sequence[str],
    *,
    max_bytes: int = MAX_QUERY_BYTES,
    timeout: float = QUERY_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Read both child pipes within one shared byte budget and deadline.

    Parameters
    ----------
    argv : Sequence[str]
        The validated journal command, executed without a shell.
    max_bytes : int
        Combined stdout and stderr byte limit; injectable for transport tests.
    timeout : float
        Maximum time to read and reap the query child.

    Returns
    -------
    subprocess.CompletedProcess[str]
        UTF-8 output and the query child's exit status.

    Raises
    ------
    JournalOutputError
        If the byte limit is exceeded or output cannot be decoded.
    subprocess.TimeoutExpired
        If the query deadline expires.
    """  # ruff: ignore[docstring-extraneous-exception] - raised by _capture and child.wait.
    deadline = time.monotonic() + timeout
    with subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed command and validated options from metrics_query.
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        try:
            stdout, stderr = _capture(child, max_bytes, deadline, timeout)
            status = child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except (
            BaseException
        ):  # Reap only this owned query child, including on interruption.
            child.kill()
            child.wait()
            raise
    try:
        return subprocess.CompletedProcess(
            argv, status, stdout.decode("utf-8"), stderr.decode("utf-8")
        )
    except UnicodeError as error:
        msg = "journal query output was not UTF-8"
        raise JournalOutputError(msg) from error


def _capture(
    child: subprocess.Popen[bytes],
    max_bytes: int,
    deadline: float,
    timeout: float,
) -> tuple[bytes, bytes]:
    """Drain both pipes without allocating beyond the shared byte budget.

    Returns
    -------
    tuple[bytes, bytes]
        Captured stdout and stderr, bounded before decoding.

    Raises
    ------
    JournalOutputError
        If output exceeds the combined byte limit.
    subprocess.TimeoutExpired
        If the read deadline expires.
    RuntimeError
        If the caller did not create the expected output pipes.
    """  # ruff: ignore[docstring-extraneous-exception] - RuntimeError is raised by _register_output_pipes.
    buffers = (bytearray(), bytearray())
    size = 0
    with selectors.DefaultSelector() as selector:
        _register_output_pipes(selector, child)
        while selector.get_map() and not (
            (remaining := deadline - time.monotonic()) <= 0
        ):
            for key, _mask in selector.select(remaining):
                block = _read_bounded(key.fd, max_bytes - size)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                size += len(block)
                buffers[key.data].extend(block)
        if selector.get_map():
            raise subprocess.TimeoutExpired(child.args, timeout)
    return bytes(buffers[0]), bytes(buffers[1])


def _read_bounded(fd: int, available: int) -> bytes:
    """Read within the remaining shared budget, probing one byte for overflow.

    Returns
    -------
    bytes
        A block within the remaining budget, or empty bytes at EOF.

    Raises
    ------
    JournalOutputError
        If a non-empty read exceeds the remaining byte budget.
    """
    block = os.read(fd, min(65536, available + 1))
    if block and len(block) > available:
        msg = "journal query exceeded its output byte limit"
        raise JournalOutputError(msg)
    return block


def _register_output_pipes(
    selector: selectors.BaseSelector, child: subprocess.Popen[bytes]
) -> None:
    """Register stdout then stderr with their capture buffer indices.

    Raises
    ------
    RuntimeError
        If the caller did not create either expected output pipe.
    """
    for index, stream in enumerate((child.stdout, child.stderr)):
        if stream is None:
            msg = "journal query requires output pipes"
            raise RuntimeError(msg)
        selector.register(stream, selectors.EVENT_READ, index)
