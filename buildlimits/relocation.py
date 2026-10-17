"""Headroom-safe descriptor relocation for launch children.

The planner assumes unrelated inherited descriptors have already been closed.
It places acyclic mappings directly and breaks cycles with one lowest free
non-target slot. Retained error channels move before any target can overwrite
them. No step depends on a parking floor above the largest descriptor.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass(frozen=True, slots=True)
class Copy:
    """Duplicate a source onto a specific slot with explicit inheritance.

    Attributes
    ----------
    source : int
        Open source descriptor.
    target : int
        Destination descriptor.
    inheritable : bool
        Whether the destination survives exec.
    """

    source: int
    target: int
    inheritable: bool


@dc.dataclass(frozen=True, slots=True)
class Close:
    """Close a source after its contents have been placed safely.

    Attributes
    ----------
    fd : int
        Descriptor whose ownership ends at this step.
    """

    fd: int


type Step = Copy | Close


@dc.dataclass(frozen=True, slots=True)
class RelocationPlan:
    """Validated relocation operations and their required descriptor ceiling.

    Attributes
    ----------
    steps : tuple[Step, ...]
        Operations in execution order, including identity inheritance.
    kept : tuple[int, ...]
        Final retained close-on-exec channels.
    required_soft : int
        Minimum soft limit needed for duplication destinations.
    kept_after : tuple[tuple[int, ...], ...]
        Retained channel locations after each successful operation.
    """

    steps: tuple[Step, ...]
    kept: tuple[int, ...]
    required_soft: int
    kept_after: tuple[tuple[int, ...], ...]


def _free_slot(occupied: set[int], targets: set[int]) -> int:
    """Select the lowest vacant non-target slot without allocating a handle.

    Returns
    -------
    int
        Lowest unoccupied descriptor that is not a compiler target.
    """
    slot = 0
    while slot in occupied or slot in targets:
        slot += 1
    return slot


def relocation_plan(
    received: cabc.Sequence[int],
    targets: cabc.Sequence[int],
    *,
    keep: cabc.Sequence[int] = (),
    hard: int = -1,
) -> RelocationPlan:
    """Plan safe placement using at most one scratch descriptor per cycle.

    Parameters
    ----------
    received : Sequence[int]
        Distinct open sources owned by the child.
    targets : Sequence[int]
        Distinct non-negative compiler descriptor numbers.
    keep : Sequence[int]
        Distinct retained channels, disjoint from sources.
    hard : int
        Supervisor's reachable hard limit; -1 means unlimited.

    Returns
    -------
    RelocationPlan
        Pure operations and the minimum duplication limit.

    Raises
    ------
    ValueError
        If ownership, target counts or reachable slots are invalid.

    Examples
    --------
    A swap uses a low scratch slot while retaining the error channel:

    >>> plan = relocation_plan([3, 4], [4, 3], keep=[5], hard=64)
    >>> plan.required_soft, plan.kept
    (5, (5,))
    """
    _validate(received, targets, keep)
    occupied = set(received) | set(keep)
    steps: list[Step] = []
    kept = _retain(keep, occupied, set(targets), steps)
    _place(received, targets, occupied, steps)
    required = _required_soft(steps)
    highest = max(targets, default=-1) + 1
    if hard != -1 and max(required, highest) > hard:
        msg = f"descriptor placement exceeds reachable hard limit {hard}"
        raise ValueError(msg)
    return RelocationPlan(tuple(steps), tuple(kept), required, _history(steps, keep))


def _required_soft(steps: cabc.Sequence[Step]) -> int:
    """Compute duplication headroom without imposing a ceiling on identity steps.

    Returns
    -------
    int
        Minimum soft limit needed by non-identity duplication operations.
    """
    return max(
        (
            step.target + 1
            for step in steps
            if isinstance(step, Copy) and step.source != step.target
        ),
        default=0,
    )


def _validate(
    received: cabc.Sequence[int], targets: cabc.Sequence[int], keep: cabc.Sequence[int]
) -> None:
    """Validate distinct descriptor ownership and the source-to-target contract.

    Raises
    ------
    ValueError
        If counts, descriptor ownership or target numbers are invalid.
    """
    if len(received) != len(targets):
        msg = "each received descriptor needs exactly one target"
        raise ValueError(msg)
    if len(set(targets)) != len(targets):
        msg = "descriptor targets must be distinct"
        raise ValueError(msg)
    _validate_numbers([*received, *keep], targets)


def _validate_numbers(owned: list[int], targets: cabc.Sequence[int]) -> None:
    """Require disjoint ownership and non-negative descriptor numbers.

    Raises
    ------
    ValueError
        If ownership aliases or any descriptor number is negative.
    """
    if len(set(owned)) != len(owned) or any(fd < 0 for fd in [*owned, *targets]):
        msg = "descriptor sources must be distinct and all numbers non-negative"
        raise ValueError(msg)


def _retain(
    keep: cabc.Sequence[int], occupied: set[int], targets: set[int], steps: list[Step]
) -> list[int]:
    """Move retained channels before placing anything onto compiler targets.

    Returns
    -------
    list[int]
        Retained channel locations after removing target collisions.
    """
    kept = list(keep)
    for index, fd in enumerate(keep):
        if fd in targets:
            kept[index] = _park(fd, occupied, targets, steps)
        else:
            steps.append(Copy(fd, fd, inheritable=False))
    return kept


def _place(
    received: cabc.Sequence[int],
    targets: cabc.Sequence[int],
    occupied: set[int],
    steps: list[Step],
) -> None:
    """Place safe mappings directly and break each cycle with one free slot."""
    pending = dict(zip(received, targets, strict=True))
    target_set = set(targets)
    while pending:
        movable = next(
            (src for src, dst in pending.items() if src == dst or dst not in pending),
            None,
        )
        if movable is None:
            _break_cycle(pending, occupied, target_set, steps)
            continue
        _place_target(movable, pending.pop(movable), occupied, steps)


def _place_target(
    source: int, target: int, occupied: set[int], steps: list[Step]
) -> None:
    """Place a safe compiler mapping before ending non-target source ownership."""
    steps.append(Copy(source, target, inheritable=True))
    occupied.add(target)
    if source != target:
        steps.append(Close(source))
        occupied.remove(source)


def _park(fd: int, occupied: set[int], targets: set[int], steps: list[Step]) -> int:
    """Move one descriptor to the lowest vacant non-target slot.

    Returns
    -------
    int
        The slot the descriptor now occupies; ``occupied`` is updated to match.
    """
    slot = _free_slot(occupied, targets)
    steps.extend((Copy(fd, slot, inheritable=False), Close(fd)))
    occupied.remove(fd)
    occupied.add(slot)
    return slot


def _break_cycle(
    pending: dict[int, int], occupied: set[int], targets: set[int], steps: list[Step]
) -> None:
    """Park one cycle source in the lowest vacant non-target slot."""
    source = next(iter(pending))
    pending[_park(source, occupied, targets, steps)] = pending.pop(source)


def _history(
    steps: cabc.Sequence[Step], keep: cabc.Sequence[int]
) -> tuple[tuple[int, ...], ...]:
    """Record every retained-channel location after each successful operation.

    Returns
    -------
    tuple[tuple[int, ...], ...]
        Channel ownership at each operation boundary, including partial plans.
    """
    locations = list(keep)
    history = []
    for step in steps:
        if isinstance(step, Copy) and step.source != step.target:
            locations = [step.target if fd == step.source else fd for fd in locations]
        history.append(tuple(locations))
    return tuple(history)
