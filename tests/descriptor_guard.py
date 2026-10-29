"""Fail any test that leaves a descriptor for the garbage collector to close.

A descriptor-owning object that nobody closes is released whenever the
collector next runs, which under xdist is during whichever test the worker
happens to be running. That test's descriptor baseline then differs from its
own, and the failure appears on a test that did nothing wrong. This guard moves
the failure to the test that opened the descriptor.

:func:`no_leaks` suspends the cyclic collector for the length of one test and
compares the process's descriptors before and after. With collection
suspended, an object kept alive only by a reference cycle cannot be reclaimed
mid-test, so the comparison is deterministic rather than dependent on
allocation timing. Objects freed by reference counting close at once and are
caught by the ``ResourceWarning`` filter in ``pyproject.toml`` instead.

The fixture at the foot of the module applies the guard to every test and
doctest.

Examples
--------
>>> import os
>>> with no_leaks():
...     read, write = os.pipe()
...     os.close(read)
...     os.close(write)
>>> try:
...     with no_leaks():
...         read, write = os.pipe()
... except DescriptorLeakError as error:
...     print("pipe" in str(error))
...     os.close(read)
...     os.close(write)
True
"""

import contextlib
import gc
import typing as typ
from pathlib import Path

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_FD_DIRECTORY = "/proc/self/fd"

type Descriptors = dict[int, str]


class DescriptorLeakError(AssertionError):
    """A test finished holding descriptors that it opened and did not close."""


def describe_descriptors() -> Descriptors:
    """Map each open descriptor to what it refers to.

    The target distinguishes a descriptor from a different one that later
    reuses the same number, which a bare set of numbers cannot. The listing's
    own directory descriptor has closed by the time it is read and is skipped.

    Returns
    -------
    dict[int, str]
        Descriptor number to its ``/proc`` link target, such as
        ``pipe:[1234]`` or ``anon_inode:[eventpoll]``.
    """
    found: Descriptors = {}
    # Materialize the listing first: while the scan is open its own descriptor
    # is still readable and would be reported as the caller's.
    for entry in list(Path(_FD_DIRECTORY).iterdir()):
        try:
            found[int(entry.name)] = str(entry.readlink())
        except OSError:
            continue
    return found


def opened_since(before: Descriptors, after: Descriptors) -> Descriptors:
    """Select descriptors in ``after`` that ``before`` did not already hold.

    Parameters
    ----------
    before : dict[int, str]
        The baseline.
    after : dict[int, str]
        The later reading.

    Returns
    -------
    dict[int, str]
        Descriptors that are new, or whose number now names a different target.
    """
    return {
        number: target
        for number, target in after.items()
        if before.get(number) != target
    }


@contextlib.contextmanager
def no_leaks() -> cabc.Generator[None]:
    """Require the block to close every descriptor it opens, without the collector.

    Raises
    ------
    DescriptorLeakError
        On exit, naming each descriptor that is still open. The collector then
        runs once, so the leak is charged to this block rather than carried
        into the next test.
    """
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    before = describe_descriptors()
    try:
        yield
    finally:
        leaked = opened_since(before, describe_descriptors())
        if was_enabled:
            gc.enable()
        if leaked:
            gc.collect()
    if leaked:
        listing = ", ".join(f"{fd}={target}" for fd, target in sorted(leaked.items()))
        msg = (
            f"the test left {len(leaked)} descriptor(s) open for the garbage "
            f"collector to close: {listing}. Close what the test opens with a "
            "context manager or a yield fixture, so no later test inherits it."
        )
        raise DescriptorLeakError(msg)


@pytest.fixture(autouse=True)
def descriptor_leak_guard() -> cabc.Iterator[None]:
    """Fail the test that leaves a descriptor for the garbage collector.

    Registered as a plugin (``-p descriptor_guard`` in ``pyproject.toml``)
    rather than from a ``conftest.py``, so it covers the package's doctests as
    well as ``tests/``. An autouse fixture is set up before the others of its
    scope and so torn down after them: the guard brackets every function-scoped
    fixture. Fixtures of a wider scope are set up before the baseline is taken
    and torn down after the comparison, so their descriptors are outside it.

    Yields
    ------
    None
        Control to the test and its fixtures.
    """
    leak = None
    try:
        with no_leaks():
            yield
    except DescriptorLeakError as error:
        leak = str(error)
    # Failing outside the handler keeps pytest from printing the message twice,
    # once as the cause of itself.
    if leak is not None:
        pytest.fail(leak, pytrace=False)
