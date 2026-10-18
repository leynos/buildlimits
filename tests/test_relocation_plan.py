"""Pure placement and ownership properties for the headroom-safe planner.

A descriptor-table model verifies file identity, exact ownership, channel
tracking and scratch bounds across generated mappings. Run with ``uv run
pytest -v
tests/test_relocation_plan.py`` from the repository root.
"""

import dataclasses as dc

import pytest
from hypothesis import given
from hypothesis import strategies as st

from buildlimits.relocation import Close, Copy, relocation_plan


@given(
    st.lists(st.integers(0, 40), max_size=8, unique=True).flatmap(
        lambda sources: st.tuples(
            st.just(sources),
            st.lists(
                st.integers(0, 40),
                min_size=len(sources),
                max_size=len(sources),
                unique=True,
            ),
        )
    )
)
def test_relocation_preserves_files_channels_and_ownership(
    pair: tuple[list[int], list[int]],
) -> None:
    """Every generated permutation ends with exactly its targets and writer."""
    received, targets = pair
    writer = max(received, default=-1) + 1
    initial = {fd: f"file-{index}" for index, fd in enumerate(received)}
    initial[writer] = "writer"
    table = dict(initial)
    plan = relocation_plan(received, targets, keep=[writer], hard=64)
    model = _DescriptorModel(table, targets, writer)
    for step, channels in zip(plan.steps, plan.kept_after, strict=True):
        model.apply(step)
        assert channels == (model.channel,), (
            "the planner records each intermediate writer location"
        )
    channel = model.channel
    inheritable = model.inheritable
    assert {fd: table[fd] for fd in targets} == {
        fd: f"file-{i}" for i, fd in enumerate(targets)
    }, "every target retains its intended open file"
    assert set(table) == {*targets, channel}, "no source or scratch ownership survives"
    assert channel not in targets and table[channel] == "writer", (
        "the writer never becomes a compiler target"
    )
    assert all(inheritable[fd] for fd in targets) and not inheritable[channel], (
        "targets inherit and the channel closes on exec"
    )
    assert plan.kept == (channel,), "tracked final channel agrees with the planner"
    assert plan.required_soft <= 64, "all duplication destinations stay reachable"
    assert model.peak <= len(initial) + 1, (
        "relocation needs at most one additional descriptor"
    )


@pytest.mark.parametrize(
    ("sources", "targets", "keep"),
    [
        ([3], [], []),
        ([3, 4], [1, 1], []),
        ([3, 3], [1, 2], []),
        ([3], [-1], []),
        ([3], [1], [3]),
    ],
)
def test_invalid_ownership_is_rejected_before_operations(
    sources: list[int], targets: list[int], keep: list[int]
) -> None:
    """Invalid counts, aliases and negative targets must not produce a plan."""
    with pytest.raises(ValueError, match=r"each received|distinct|non-negative"):
        relocation_plan(sources, targets, keep=keep, hard=64)


def test_targets_at_the_hard_limit_are_unreachable() -> None:
    """A target equal to hard is refused even when its source is already there."""
    with pytest.raises(ValueError, match="reachable hard limit"):
        relocation_plan([64], [64], hard=64)


@dc.dataclass(slots=True)
class _DescriptorModel:
    """Track file identity, inheritance and writer ownership after each operation."""

    table: dict[int, str]
    targets: list[int]
    channel: int
    inheritable: dict[int, bool] = dc.field(default_factory=dict)
    peak: int = dc.field(init=False)

    def __post_init__(self) -> None:
        """Include the initial descriptor table in the scratch-space high-water mark."""
        self.peak = len(self.table)

    def apply(self, step: Copy | Close) -> None:
        """Apply a model operation while checking exactly-once source ownership."""
        match step:
            case Copy():
                self._copy(step)
            case Close(fd=fd):
                assert fd in self.table, "each source is closed exactly once"
                del self.table[fd]
                self.inheritable.pop(fd, None)
        self.peak = max(self.peak, len(self.table))

    def _copy(self, step: Copy) -> None:
        """Verify lowest-free scratch selection before transferring file identity."""
        assert step.source in self.table, "only still-owned descriptors are copied"
        if not step.inheritable and step.source != step.target:
            free = next(
                fd
                for fd in range(64)
                if fd not in self.table and fd not in self.targets
            )
            assert step.target == free, "scratch is the lowest free non-target slot"
        self.table[step.target] = self.table[step.source]
        self.inheritable[step.target] = step.inheritable
        if step.source == self.channel:
            self.channel = step.target
