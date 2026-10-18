"""The leak guard reports what only the collector would close, and nothing else."""

import os
import selectors
import typing as typ

import pytest
from cache_recovery_harness import harness_for
from descriptor_guard import (
    DescriptorLeakError,
    describe_descriptors,
    no_leaks,
    opened_since,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def _selector_kept_by_a_cycle() -> None:
    """Open an epoll selector that only the cyclic collector can reclaim."""
    cycle: list[object] = []
    cycle.extend((cycle, selectors.DefaultSelector()))


def test_opened_since_reports_new_descriptors_and_reused_numbers() -> None:
    """A number that now names another target counts as new."""
    before = {0: "a", 1: "b"}
    after = {0: "a", 1: "c", 2: "d"}
    assert opened_since(before, after) == {1: "c", 2: "d"}, (
        "unchanged descriptors are ignored; replaced and added ones are reported"
    )


def test_a_block_that_closes_everything_passes() -> None:
    """Descriptors opened and closed inside the block are not leaks."""
    with no_leaks():
        read, write = os.pipe()
        os.close(read)
        os.close(write)


def test_an_object_only_the_collector_would_close_is_reported() -> None:
    """A selector kept alive by a cycle is charged to the block that made it."""
    before = describe_descriptors()
    with pytest.raises(DescriptorLeakError, match="eventpoll"):
        with no_leaks():
            _selector_kept_by_a_cycle()
    assert opened_since(before, describe_descriptors()) == {}, (
        "the guard collected the leak so the next test does not inherit it"
    )


def test_an_unclosed_recovery_harness_is_a_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor a harness builds owns an epoll descriptor to close."""
    with pytest.raises(DescriptorLeakError, match="eventpoll"):
        with no_leaks():
            harness_for(tmp_path, monkeypatch)


def test_a_closed_recovery_harness_is_not_a_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing the harness returns the epoll descriptor deterministically."""
    with no_leaks(), harness_for(tmp_path, monkeypatch):
        pass
