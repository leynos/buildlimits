"""Fault injectors for the cache-outcome writer's synchronous seams.

The writer's append reaches two seams a test can hold open or refuse: the
queue's ``append_line`` call and the filesystem's ``os.write``. These helpers
keep those faults in one place, apart from the factories and result waits the
other store helpers provide.
"""

import contextlib
import threading
import typing as typ
from unittest import mock

from buildlimits import outcome_store_io, outcome_store_state

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


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
            release.wait()
        append_line(store, line)

    try:
        with mock.patch.object(outcome_store_state, "append_line", side_effect=stall):
            yield entered, release
    finally:
        release.set()


@contextlib.contextmanager
def partial_append() -> cabc.Generator[None]:
    """Fail an append after half its bytes reach the file.

    The refusal models a disk that fills part-way through a row, leaving a
    fragment the writer must roll back. A caller adds its own ``ftruncate``
    fault when the rollback itself must fail.

    Yields
    ------
    None
        The patch is active for the context's body.
    """
    write = outcome_store_io.os.write

    def write_part_then_fail(descriptor: int, data: bytes) -> int:
        """Leave half a record on disk, then fail its append.

        Raises
        ------
        OSError
            The append is refused after its partial write.
        """
        write(descriptor, data[: len(data) // 2])
        msg = "the test's disk fills mid-record"
        raise OSError(msg)

    with mock.patch.object(outcome_store_io.os, "write", write_part_then_fail):
        yield
