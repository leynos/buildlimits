"""Waits for the asynchronous results a store writer publishes.

The writer reports outcomes through a queue that aggregates equal results, so
a test that needs to know an operation finished — not merely that it started —
has to wait for a result *and* account for the rows it acknowledges. These
helpers keep that policy in one place: ``wait_for_results`` returns at the
first match, and ``wait_for_records`` counts acknowledged records so a
two-row operation cannot be mistaken for a one-row one.
"""

import threading
import time
import typing as typ
import weakref

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_ports import StoreResult
    from buildlimits.outcome_store import StoreWriter

__all__ = ["wait_for_records", "wait_for_results"]

_WAIT_SECONDS = 2.0
_RESULT_BACKLOG: weakref.WeakKeyDictionary[StoreWriter, list[StoreResult]] = (
    weakref.WeakKeyDictionary()
)


def _matches(result: StoreResult, operation: str, reason: str | None) -> bool:
    """Return whether one published result records the expected operation.

    Returns
    -------
    bool
        Whether the operation matches, and the reason too unless it is None.
    """
    return result.operation == operation and (reason is None or result.reason == reason)


def wait_for_results(
    writer: StoreWriter,
    *,
    operation: str,
    reason: str | None = None,
) -> tuple[StoreResult, ...]:
    """Wait for one result and retain concurrently drained unrelated results.

    Returns
    -------
    tuple[StoreResult, ...]
        The collected results, including unrelated values drained meanwhile.
    """
    deadline = time.monotonic() + _WAIT_SECONDS
    observed = _RESULT_BACKLOG.pop(writer, [])
    while time.monotonic() < deadline:
        observed.extend(writer.results())
        matching = [
            result for result in observed if _matches(result, operation, reason)
        ]
        if matching:
            _RESULT_BACKLOG[writer] = [
                result for result in observed if result not in matching
            ]
            return tuple(observed)
        threading.Event().wait(0.01)
    _RESULT_BACKLOG[writer] = observed
    return pytest.fail(f"writer did not report {operation} with reason {reason!r}")


def wait_for_records(
    writer: StoreWriter,
    expected: StoreResult,
    *,
    observed: cabc.Iterable[StoreResult] = (),
) -> tuple[StoreResult, ...]:
    """Wait until matching results acknowledge every record ``expected`` names.

    ``wait_for_results`` returns at the first match. That is enough to know an
    operation ran, but not that every row it covers is on disk: a seal the
    second append triggers publishes its result before the row it closed is
    written, so a test that waited only for the seal could read the file
    before the second append completed.

    The writer aggregates results by operation and reason, so two appends
    drained together arrive as one result whose ``records`` count is two.
    ``expected.records`` therefore counts acknowledged records rather than
    results. Results the caller already drained through ``observed`` count
    too, and identity tracking stops one passed in from being counted twice
    when a later wait returns it again.

    Parameters
    ----------
    writer : StoreWriter
        The writer to drain.
    expected : StoreResult
        The operation and reason to count, and how many records the matching
        results must acknowledge. Its ``discarded`` count is ignored.
    observed : Iterable[StoreResult]
        Results the caller has already drained, counted towards the total.

    Returns
    -------
    tuple[StoreResult, ...]
        Every distinct result observed, including the ones passed in.

    Examples
    --------
    Both appends of a two-row seal complete before the test reads the store::

        results = seal_two_rows(writer, reason="save_failed")
        wait_for_records(writer, StoreResult("append", "none", 2, 0), observed=results)
    """
    collected = list(observed)
    seen = {id(result) for result in collected}
    while (
        _acknowledged(collected, expected.operation, expected.reason) < expected.records
    ):
        for result in wait_for_results(
            writer, operation=expected.operation, reason=expected.reason
        ):
            if id(result) not in seen:
                seen.add(id(result))
                collected.append(result)
    return tuple(collected)


def _acknowledged(
    results: cabc.Iterable[StoreResult], operation: str, reason: str | None
) -> int:
    """Return how many records the matching results acknowledge.

    Returns
    -------
    int
        The sum of the matching results' ``records`` counters.
    """
    return sum(
        result.records for result in results if _matches(result, operation, reason)
    )
