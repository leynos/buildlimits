"""Isolate descriptor-pressure experiments from the parent test runner.

``open_fds`` verifies descriptor ownership after enumeration. ``isolated``
opens its reporting pipe before fork, lowers only the child's limit, and checks
parent descriptor and limit recovery. Expected syscall failures are structured
results; other exceptions fail the experiment.

Examples
--------
>>> baseline = open_fds()
>>> isolated(lambda _report: 42)
42
>>> open_fds() == baseline
True
"""

import contextlib
import dataclasses as dc
import errno
import json
import os
import resource
import signal
import typing as typ
from pathlib import Path

from buildlimits.descriptors import close_all

if typ.TYPE_CHECKING:
    import collections.abc as cabc


SOFT_LIMIT = 64


@dc.dataclass(slots=True)
class _ReportChild:
    """Track the experiment child until its status has been collected."""

    pid: int
    reaped: bool = False


def open_fds() -> set[int]:
    """Return verified open descriptors, excluding the listing's descriptor.

    Directory enumeration needs a free descriptor and propagates its I/O errors.

    Returns
    -------
    set[int]
        Descriptor numbers that remain open after enumeration.

    """
    listed = [int(item.name) for item in Path("/proc/self/fd").iterdir()]
    found = set()
    for fd in listed:
        try:
            os.fstat(fd)
        except OSError:
            continue
        found.add(fd)
    return found


def isolated(action: cabc.Callable[[int], object], soft: int = SOFT_LIMIT) -> object:
    """Run an experiment with a low child limit and verify parent isolation.

    Report collection propagates RuntimeError when the child raises an
    unexpected exception or cannot finish its report.

    Parameters
    ----------
    action : Callable[[int], object]
        Experiment receiving a reporting descriptor to preserve.
    soft : int
        Child's descriptor soft limit.

    Returns
    -------
    object
        JSON result, including bounded details for expected syscall failures.

    Raises
    ------
    BaseExceptionGroup
        If parent isolation fails; an original report failure remains included.
    """
    baseline = open_fds()
    limits = resource.getrlimit(resource.RLIMIT_NOFILE)
    read, write, child = _fork_reporter(action, soft, limits)
    os.close(write)
    result = None
    failure = None
    try:
        result = _collect_report(read, child)
    except BaseException as error:  # ruff: ignore[blind-except] - preserve the report failure while checking isolation.
        failure = error
    finally:
        _finish_reporter(read, child)
    try:
        _check_parent(limits, baseline)
    except BaseException as parent_error:  # ruff: ignore[blind-except] - retain both independent failure causes.
        errors = [parent_error] if failure is None else [failure, parent_error]
        message = "experiment isolation failed"
        raise BaseExceptionGroup(message, errors) from None
    if failure is not None:
        raise failure
    return result


def _fork_reporter(
    action: cabc.Callable[[int], object], soft: int, limits: tuple[int, int]
) -> tuple[int, int, _ReportChild]:
    """Allocate reporting channels and close both if creating the child fails.

    Returns
    -------
    tuple[int, int, _ReportChild]
        Parent-owned reporting reader, writer and unreaped child.
    """
    read, write = os.pipe()
    try:
        pid = os.fork()
    except BaseException:
        for fd in (write, read):
            with contextlib.suppress(OSError):
                os.close(fd)
        raise
    if pid == 0:  # pragma: no cover - isolated child
        _run_child(action, read, write, (soft, limits[1]))
    return read, write, _ReportChild(pid)


def _finish_reporter(read: int, child: _ReportChild) -> None:
    """Close the reader and terminate an interrupted experiment before reaping."""
    with contextlib.suppress(OSError):
        os.close(read)
    if not child.reaped:
        try:
            ended, _status = os.waitpid(child.pid, os.WNOHANG)
        except ChildProcessError:
            child.reaped = True
            return
        if ended:
            child.reaped = True
            return
        with contextlib.suppress(ProcessLookupError):
            os.kill(child.pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(child.pid, 0)
        child.reaped = True


def _run_child(
    action: cabc.Callable[[int], object], read: int, write: int, limits: tuple[int, int]
) -> typ.NoReturn:
    """Run the child protocol and exit nonzero unless its report completes."""
    status = 1
    try:
        os.close(read)
        resource.setrlimit(resource.RLIMIT_NOFILE, limits)
        result, action_status = _action_result(action, write)
        payload, serialization_status = _report_payload(result)
        complete = _write_report(write, payload)
        status = action_status or serialization_status or int(not complete)
    finally:
        os._exit(status)


def _collect_report(read: int, child: _ReportChild) -> object:
    """Drain the report, reap the child, and decode only a successful result.

    Returns
    -------
    object
        JSON result after successful child exit.

    Raises
    ------
    RuntimeError
        If the child exits unsuccessfully, with a bounded report excerpt.
    """
    chunks = []
    while chunk := os.read(read, 4096):
        chunks.append(chunk)
    payload = b"".join(chunks)
    _, status = os.waitpid(child.pid, 0)
    child.reaped = True
    if status != 0:
        msg = f"disposable child failed (status={status}): {payload[:512]!r}"
        raise RuntimeError(msg)
    return json.loads(payload)


def _check_parent(limits: tuple[int, int], baseline: set[int]) -> None:
    """Require unchanged parent limits and descriptors after pipe cleanup.

    Raises
    ------
    AssertionError
        If the isolated experiment changes its parent's resource ownership.
    """
    if resource.getrlimit(resource.RLIMIT_NOFILE) != limits:
        msg = "parent limits are unchanged"
        raise AssertionError(msg)
    if open_fds() != baseline:
        msg = "parent descriptors recover to baseline"
        raise AssertionError(msg)


def still_open(rights: cabc.Iterable[int]) -> list[int]:
    """Probe owned descriptors with ``fstat`` and return those still open.

    A probe that fails with anything but ``EBADF`` is re-raised, so a leak is
    never mistaken for a closed descriptor.

    Parameters
    ----------
    rights : Iterable[int]
        Descriptor numbers that should have been closed.

    Returns
    -------
    list[int]
        The descriptors still open, in the order given.

    Raises
    ------
    OSError
        If a probe fails for any reason other than ``EBADF``.
    """
    remaining = []
    for fd in rights:
        try:
            os.fstat(fd)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            remaining.append(fd)
    return remaining


def fill_table() -> list[int]:
    """Fill a disposable child's descriptor table up to its soft limit.

    Returns
    -------
    list[int]
        Owned handles to close after the pressure experiment.

    Raises
    ------
    OSError
        If opening a descriptor fails for a reason other than EMFILE.
    """
    filled = []
    try:
        while True:
            filled.append(os.open("/dev/null", os.O_RDWR))
    except OSError as error:
        if error.errno == errno.EMFILE:
            return filled
        close_all(filled)
        raise


def _action_result(
    action: cabc.Callable[[int], object], write: int
) -> tuple[object, int]:
    """Turn disposable action failures into bounded data and an exit-status flag.

    Returns
    -------
    tuple[object, int]
        Result and failure flag; expected syscall errors remain successful evidence.
    """
    try:
        return action(write), 0
    except BaseException as error:  # ruff: ignore[blind-except] - serialize every disposable child failure.
        return {
            "error": type(error).__name__,
            "errno": error.errno if isinstance(error, OSError) else None,
        }, int(not isinstance(error, OSError))


def _report_payload(result: object) -> tuple[bytes, int]:
    """Encode a child result or record a serialization failure explicitly.

    Returns
    -------
    tuple[bytes, int]
        ASCII JSON and whether encoding the original result failed.
    """
    try:
        return json.dumps(result).encode("ascii"), 0
    except (TypeError, ValueError) as error:
        return json.dumps({"error": type(error).__name__, "errno": None}).encode(
            "ascii"
        ), 1


def _write_report(write: int, payload: bytes) -> bool:
    """Finish partial pipe writes without claiming success after a zero-byte write.

    Returns
    -------
    bool
        Whether all reporting bytes were written.
    """
    while payload:
        written = os.write(write, payload)
        if written == 0:
            return False
        payload = payload[written:]
    return True
