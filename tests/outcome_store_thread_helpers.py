"""Thread-coordination fixtures for the outcome-store writer tests.

The writer appends on its own worker, so a test that inspects the moment an
append returns, or stalls it partway, has to coordinate with that worker. These
two context managers patch the writer's file operations; they live apart from
the store factories so each module has one concern.
"""

import contextlib
import threading
import typing as typ
from unittest import mock

from buildlimits import outcome_store_state

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreRecord


@contextlib.contextmanager
def append_completion_barrier(finished: threading.Event) -> cabc.Generator[None]:
    """Mark an event once a writer append, post-append ageing included, returns.

    ``StoreFiles.append`` publishes its success result before it reads the
    clock to decide whether the oldest row has aged. A test that advanced the
    clock on the result could move it before that read, so the worker would
    observe a different skew or cutoff. The event is set after the append.

    Parameters
    ----------
    finished : threading.Event
        The event the test clears before each append and waits on afterwards.

    Yields
    ------
    None
        While the writer's append publishes ``finished`` when it returns.
    """
    append = outcome_store_state.StoreFiles.append

    def append_and_signal(
        files: outcome_store_state.StoreFiles,
        line: bytes,
        record: StoreRecord,
    ) -> None:
        """Run the writer's append, then mark its post-append ageing done."""
        try:
            append(files, line, record)
        finally:
            finished.set()

    with mock.patch.object(outcome_store_state.StoreFiles, "append", append_and_signal):
        yield


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
        """Delay the first append before calling the underlying writer.

        The wait has no timeout: a bound that expired would let the worker
        write and drain the queue while the test still believed it was stalled,
        which turns a slow host into a spurious failure. The context's teardown
        always sets ``release``, so a failing test cannot wedge the worker.
        """
        if not entered.is_set():
            entered.set()
            release.wait()
        append_line(store, line)

    try:
        with mock.patch.object(outcome_store_state, "append_line", side_effect=stall):
            yield entered, release
    finally:
        release.set()
