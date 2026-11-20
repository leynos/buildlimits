"""Shared factories and asynchronous result waits for outcome-store tests."""

import contextlib
import threading
import time
import typing as typ
import weakref
from unittest import mock

import pytest

from buildlimits import outcome_records, outcome_store_state
from buildlimits.outcome_ports import StoreResult
from buildlimits.outcome_store import StoreWriter

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    from buildlimits.outcome_key import Salt
    from buildlimits.outcome_ports import StoreFailure
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_vocabulary import StoreReason

_SALT_NAME = "cache-outcomes.salt"
_WAIT_SECONDS = 2.0
_RESULT_BACKLOG: weakref.WeakKeyDictionary[StoreWriter, list[StoreResult]] = (
    weakref.WeakKeyDictionary()
)


def wait_for_salt(writer: StoreWriter) -> Salt | StoreFailure:
    """Wait for the writer's startup salt handoff during a test.

    Returns
    -------
    Salt or StoreFailure
        The writer's startup result.
    """
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        result = writer.salt_result()
        if result is not None:
            return result
        threading.Event().wait(0.01)
    return pytest.fail("writer did not publish its salt result")


def wait_for_results(
    writer: StoreWriter,
    *,
    operation: str,
    reason: str | None = None,
    timeout: float = _WAIT_SECONDS,
) -> tuple[StoreResult, ...]:
    """Wait for one result and retain concurrently drained unrelated results.

    A test that seeds a multi-megabyte store passes a longer ``timeout``: the
    startup pass over ten thousand rows can outlast the default wait when the
    host is busy, which is load, not a writer fault.

    Parameters
    ----------
    writer : StoreWriter
        The writer whose published results are drained.
    operation : str
        The result operation to wait for.
    reason : str | None
        A required closed reason, or None to accept any reason.
    timeout : float
        Seconds to allow the writer to publish the result.

    Returns
    -------
    tuple[StoreResult, ...]
        The collected results, including unrelated values drained meanwhile.
    """
    deadline = time.monotonic() + timeout
    observed = _RESULT_BACKLOG.pop(writer, [])
    while time.monotonic() < deadline:
        observed.extend(writer.results())
        matching = [
            result
            for result in observed
            if result.operation == operation
            and (reason is None or result.reason == reason)
        ]
        if matching:
            _RESULT_BACKLOG[writer] = [
                result for result in observed if result not in matching
            ]
            return tuple(observed)
        threading.Event().wait(0.01)
    _RESULT_BACKLOG[writer] = observed
    return pytest.fail(f"writer did not report {operation} with reason {reason!r}")


def close_writer(writer: StoreWriter, timeout: float = 2.0) -> None:
    """Close one test writer and require its worker to stop.

    A close that only expires its caller's bound can leave a write running
    after the test ends, where it is charged to an unrelated test. Requiring
    the worker to exit makes that fault visible in the test that caused it.

    Parameters
    ----------
    writer : StoreWriter
        The writer to close.
    timeout : float
        Seconds to allow the worker to stop.

    Examples
    --------
    ``close_writer(writer)`` fails the current test if the worker is still
    running afterwards.
    """
    writer.close(timeout)
    if not worker_exited(writer, max(timeout, 1.0)):
        pytest.fail("the writer worker did not stop within its bound")


def worker_exited(writer: StoreWriter, timeout: float) -> bool:
    """Wait until the writer's worker has stopped, bounded by ``timeout``.

    ``close`` returns at its caller's bound whether or not the worker finished,
    so a test that deliberately lets that bound expire can still have a write
    running when it returns. Waiting here gives that write somewhere to finish:
    the descriptor guard samples the descriptor table as the test ends, and a
    write that resumes afterwards is charged to the test that caused it.

    This reads the worker through ``writer._thread``, the same narrow
    observation ``test_outcome_store_machine`` makes when it asserts that
    shutdown completed.

    Parameters
    ----------
    writer : StoreWriter
        The writer whose worker to wait for.
    timeout : float
        Seconds to wait before reporting that the worker is still running.

    Returns
    -------
    bool
        Whether the worker stopped within the bound.
    """
    deadline = time.monotonic() + timeout
    while writer._thread.is_alive():
        if time.monotonic() >= deadline:
            return False
        threading.Event().wait(0.01)
    return True


def salt_path(path: Path) -> Path:
    """Return the salt path beside one cache-outcome store.

    Returns
    -------
    Path
        The sibling salt path.
    """
    return path.with_name(_SALT_NAME)


def start_writer(path: Path, *, queue_capacity: int = 4096) -> StoreWriter:
    """Start a writer for one isolated temporary store.

    Returns
    -------
    StoreWriter
        The new asynchronous writer.
    """
    return StoreWriter(path, queue_capacity=queue_capacity)


def ready_writer(path: Path, *, queue_capacity: int = 4096) -> StoreWriter:
    """Start a writer after its startup salt and compaction have completed.

    Returns
    -------
    StoreWriter
        The initialized writer, ready to accept records.

    Examples
    --------
    ``ready_writer(path)`` returns only after startup compaction completes.
    """
    writer = start_writer(path, queue_capacity=queue_capacity)
    try:
        wait_for_salt(writer)
        wait_for_results(writer, operation="compact", reason="none")
    except BaseException:
        close_writer(writer, 1.0)
        raise
    return writer


def segment_writer(
    path: Path, policy: RetentionPolicy, now: dt.datetime
) -> StoreWriter:
    """Start a segmented writer and wait until it accepts appends.

    A segmented writer publishes its startup salt and reads its retained state
    before its first append is accepted, so the wait is on ``load`` rather than
    on the legacy compaction result.

    Returns
    -------
    StoreWriter
        The initialized writer, ready to accept records.
    """
    writer = StoreWriter(path, policy=policy, wall_clock=lambda: now)
    try:
        wait_for_salt(writer)
        wait_for_results(writer, operation="load")
    except BaseException:
        close_writer(writer, 1.0)
        raise
    return writer


@contextlib.contextmanager
def running_writer(
    path: Path, *, queue_capacity: int = 4096
) -> cabc.Generator[StoreWriter]:
    """Yield an initialized writer and always close its worker afterwards.

    Yields
    ------
    StoreWriter
        The initialized writer, ready to accept records.

    Examples
    --------
    ``with running_writer(path) as writer`` closes the worker on exit.
    """
    writer = ready_writer(path, queue_capacity=queue_capacity)
    try:
        yield writer
    except BaseException:
        # Never mask the test's own failure with a shutdown assertion, but
        # still wait for the worker so a write cannot outlive the test.
        writer.close(1.0)
        worker_exited(writer, 1.0)
        raise
    close_writer(writer, 1.0)


def load_and_close(
    path: Path, *, reason: str, compact: bool = False
) -> tuple[Salt | StoreFailure, tuple[StoreResult, ...]]:
    """Load one prepared store and return its startup result before closing.

    Parameters
    ----------
    path : Path
        The store whose pre-existing contents should be loaded.
    reason : str
        Closed load reason expected from the store contents.
    compact : bool
        Whether the test expects startup compaction after loading.

    Returns
    -------
    tuple[Salt or StoreFailure, tuple[StoreResult, ...]]
        The active salt result and all startup outcomes observed.
    """
    writer = start_writer(path)
    try:
        salt = wait_for_salt(writer)
        observed = list(wait_for_results(writer, operation="load", reason=reason))
        if compact and not any(result.operation == "compact" for result in observed):
            observed.extend(
                wait_for_results(writer, operation="compact", reason="none")
            )
        return salt, tuple(observed)
    finally:
        writer.close(1.0)


def append_and_wait(
    writer: StoreWriter, record: StoreRecord
) -> tuple[StoreResult, ...]:
    """Append one typed row and wait for the writer's success result.

    Returns
    -------
    tuple[StoreResult, ...]
        The append result and any concurrently drained operations.
    """
    writer.append(record)
    return wait_for_results(writer, operation="append", reason="none")


def stored_records(path: Path) -> tuple[StoreRecord, ...]:
    """Decode and return the rows currently written to one store.

    Returns
    -------
    tuple[StoreRecord, ...]
        The validated records in file order.
    """
    return decoded_store(path).records


def decoded_store(path: Path) -> outcome_records.DecodedStore:
    """Read a valid store fixture or fail the current test clearly.

    Returns
    -------
    DecodedStore
        The validated header and records.
    """
    decoded = outcome_records.decode_lines(path.read_text(encoding="utf-8"))
    match decoded:
        case outcome_records.RecordFailure() as failure:
            pytest.fail(f"store fixture did not decode: {failure.reason}")
        case outcome_records.DecodedStore() as valid_store:
            return valid_store


def assert_failed_append(
    path: Path,
    results: tuple[StoreResult, ...],
    expected_records: tuple[StoreRecord, ...],
) -> None:
    """Check one rejected append and the rows that must remain on disk.

    Parameters
    ----------
    path : Path
        The store whose contents must remain stable after rejection.
    results : tuple[StoreResult, ...]
        Outcomes drained from the writer.
    expected_records : tuple[StoreRecord, ...]
        Complete rows that should remain after the failed append.
    """
    if StoreResult("append", "save_failed", 0, 1) not in results:
        pytest.fail("a rejected append is reported as one discarded row")
    if stored_records(path) != expected_records:
        pytest.fail("a rejected append preserves the complete rows")


def assert_load_result(
    results: tuple[StoreResult, ...], reason: StoreReason, discarded: int = 0
) -> None:
    """Check one closed load outcome and its discarded-row count.

    Parameters
    ----------
    results : tuple[StoreResult, ...]
        Outcomes drained from the writer.
    reason : str
        The closed load reason expected from the startup contents.
    discarded : int
        Number of old records excluded from the active store.
    """
    if StoreResult("load", reason, 0, discarded) not in results:
        pytest.fail(f"load reason {reason!r} reports {discarded} discarded rows")


@contextlib.contextmanager
def stalled_append() -> cabc.Generator[tuple[threading.Event, threading.Event]]:
    """Pause the first writer append until the test releases it.

    Yields
    ------
    tuple[Event, Event]
        An entered signal and a release signal for deterministic queue tests.
    """
    entered = threading.Event()
    release = threading.Event()
    append_line = outcome_store_state.append_line

    def stall(store: Path, line: bytes) -> None:
        """Delay the first append before calling the underlying writer."""
        if not entered.is_set():
            entered.set()
            release.wait(5.0)
        append_line(store, line)

    try:
        with mock.patch.object(outcome_store_state, "append_line", side_effect=stall):
            yield entered, release
    finally:
        release.set()
