"""Tests for descriptor inheritance and relocation."""

import fcntl
import json
import os
import typing as typ
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from buildlimits.descriptors import (
    apply_relocation,
    inheritable_descriptors,
    relocation_plan,
)
from buildlimits.relocation import Close, Copy


def _simulate(received: list[int], targets: list[int]) -> dict[int, str]:
    """Run a plan against a model descriptor table; return target contents."""
    table = {fd: f"file-{index}" for index, fd in enumerate(received)}
    for step in relocation_plan(received, targets).steps:
        match step:
            case Copy(source=source, target=target):
                table[target] = table[source]
            case Close(fd=fd):
                del table[fd]
    return {target: table[target] for target in targets}


@given(
    st.lists(st.integers(0, 40), min_size=0, max_size=8, unique=True).flatmap(
        lambda received: st.tuples(
            st.just(received),
            st.lists(
                st.integers(0, 40),
                min_size=len(received),
                max_size=len(received),
                unique=True,
            ),
        )
    )
)
def test_every_target_ends_up_with_its_own_file(
    pair: tuple[list[int], list[int]],
) -> None:
    """No placement clobbers a descriptor that has yet to move."""
    received, targets = pair
    result = _simulate(received, targets)
    assert result == {target: f"file-{i}" for i, target in enumerate(targets)}, (
        "every target ends up with its own file"
    )


def test_mismatched_or_repeated_targets_are_rejected() -> None:
    """A plan needs one distinct target per received descriptor."""
    with pytest.raises(ValueError, match="exactly one target"):
        relocation_plan([3], [])
    with pytest.raises(ValueError, match="distinct"):
        relocation_plan([3, 4], [1, 1])


def test_inheritable_descriptors_skip_close_on_exec() -> None:
    """Only descriptors an exec would inherit are listed."""
    keep_read, keep_write = os.pipe()
    os.set_inheritable(keep_read, True)
    closing_read, closing_write = os.pipe2(os.O_CLOEXEC)
    try:
        found = inheritable_descriptors()
        assert keep_read in found, "an inheritable descriptor is listed"
        assert closing_read not in found, "a close-on-exec descriptor is not listed"
        assert keep_read not in inheritable_descriptors(exclude={keep_read}), (
            "an excluded descriptor is omitted"
        )
    finally:
        for fd in (keep_read, keep_write, closing_read, closing_write):
            os.close(fd)


def _relocate_and_report(received: list[int], report_write: int) -> typ.NoReturn:
    """Relocate descriptors in a forked child, report what is open, and exit."""
    try:
        # The kept descriptor starts on a target number, the case that once
        # clobbered the error pipe.
        keep = os.dup(report_write)
        targets = [keep, 40, 41]
        (moved,) = apply_relocation(received, targets, keep=(keep,))
        for index, target in enumerate(targets):
            os.write(target, f"to-{index}".encode())
        leftover = sorted(int(e.name) for e in Path("/proc/self/fd").iterdir())
        flags = [fcntl.fcntl(t, fcntl.F_GETFD) & fcntl.FD_CLOEXEC for t in targets]
        os.write(moved, json.dumps([leftover, flags, moved in targets]).encode())
    finally:
        os._exit(0)


def test_relocation_in_a_child_places_and_closes() -> None:
    """In a forked child, targets hold the right files and nothing else stays."""
    pipes = [os.pipe() for _ in range(3)]
    report_read, report_write = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        _relocate_and_report([write for _read, write in pipes], report_write)
    os.close(report_write)
    for _read, write in pipes:
        os.close(write)
    os.waitpid(pid, 0)
    report = os.read(report_read, 4096).decode()
    os.close(report_read)
    leftover, flags, moved_onto_target = json.loads(report)
    assert not moved_onto_target, "the moved descriptor never lands on a target number"
    assert flags == [0, 0, 0], "target descriptors are not close-on-exec"
    assert len(leftover) == 5, (  # three targets, the kept pipe, the listing's own fd
        "nothing else stays open in the child"
    )
    for index, (read, _write) in enumerate(pipes):
        assert os.read(read, 16) == f"to-{index}".encode(), (
            "each target holds the file written under its own index"
        )
        os.close(read)
