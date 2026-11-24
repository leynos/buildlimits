"""Only a cached admission may enter the outcome recorder's window."""

import typing as typ
from pathlib import Path

from differential_trace_test_helpers import cached_entry, direct_entry
from outcome_shell_test_support import (
    OutcomeWorld,
    admitted_launch,
    make_outcome_world,
)

from buildlimits.effects import Exit
from buildlimits.supervisor_outcome_setup import GroupEnding

if typ.TYPE_CHECKING:
    from buildlimits.supervisor_outcomes import OutcomeShell


def _public_snapshot(shell: OutcomeShell, world: OutcomeWorld) -> tuple[object, ...]:
    """Return the public observations a direct launch must leave unchanged.

    Returns
    -------
    tuple[object, ...]
        The status section, journal metrics and queued store records.
    """
    return (
        shell.describe(),
        list(world.metrics.events),
        list(world.writer.records),
    )


def test_direct_launch_and_ending_enter_no_recorder_window(tmp_path: Path) -> None:
    """A direct composition passes both hooks without recording anything.

    Admission chooses the cached path only for a cacheable request while the
    cache is allowed. The shell receives the selected path as data, so a direct
    launch reaching it must be ignored rather than trusted as an observation.
    """
    world = make_outcome_world(tmp_path)
    shell = typ.cast("OutcomeShell", world.shell)
    before = _public_snapshot(shell, world)

    launch = admitted_launch(world.admission, 1, cacheable=False)
    assert not launch.cached, "a non-cacheable request is launched directly"

    shell.launched(direct_entry(), launch.group_id)
    progress = shell._recorder.state.progress
    assert progress.window.entries == (), "a direct launch opens no window entry"
    assert progress.active == (), "a direct launch holds no active recorder launch"

    world.admission.leader_exited(launch.group_id, Exit(code=0))
    world.admission.group_emptied(launch.group_id)
    shell.emptied(GroupEnding(launch.group_id, False, True))

    assert _public_snapshot(shell, world) == before, (
        "a direct launch and ending publish no status, metric or record"
    )
    progress = shell._recorder.state.progress
    assert progress.window.entries == (), "a direct ending adds no window entry"
    assert progress.active == (), "a direct ending leaves no active launch"
    assert not any(name == "cache_outcome" for name, _fields in world.metrics.events), (
        "a direct launch produces no cache-outcome event"
    )
    assert not world.writer.records, "a direct launch queues no store record"
    shell.close(timeout=0.1)


def test_cached_launch_control_opens_the_window(tmp_path: Path) -> None:
    """The same world records a cached launch, so the guard is not vacuous."""
    world = make_outcome_world(tmp_path)
    shell = typ.cast("OutcomeShell", world.shell)

    shell.launched(cached_entry(), "cached-control")

    progress = shell._recorder.state.progress
    assert len(progress.window.entries) == 1, "a cached launch opens one window entry"
    assert progress.active != (), "a cached launch is an active recorder launch"
    shell.close(timeout=0.1)


def _world_with_a_cached_launch(
    tmp_path: Path,
) -> tuple[OutcomeWorld, OutcomeShell, str]:
    """Return a world whose one cached group has launched and not yet ended."""
    world = make_outcome_world(tmp_path)
    shell = typ.cast("OutcomeShell", world.shell)
    launch = admitted_launch(world.admission, 1)
    shell.launched(cached_entry(), launch.group_id)
    return world, shell, launch.group_id


def _end_cached_group(world: OutcomeWorld, shell: OutcomeShell, group_id: str) -> None:
    """End one cached group and release the token its detached cache retained."""
    world.admission.leader_exited(group_id, Exit(code=0))
    world.admission.group_emptied(group_id)
    shell.emptied(GroupEnding(group_id, False, True))
    world.admission.server_activity(active_compiles=0)


def test_a_first_baseline_waits_for_cached_work_to_stop(tmp_path: Path) -> None:
    """A moving cached group blocks even the first baseline.

    Counters a compilation has not yet published would land in the next
    window's delta while belonging to no window, so no boundary is read while a
    cached group is in flight.
    """
    world, shell, _group_id = _world_with_a_cached_launch(tmp_path)

    shell.on_tick(0.0)

    assert not world.statistics.starts, (
        "the baseline query waits while a cached group is still moving"
    )
    assert shell._recorder.state.progress.baseline is None, "no baseline was taken"
    shell.close(timeout=0.1)


def test_a_first_baseline_starts_once_cached_work_ends(tmp_path: Path) -> None:
    """The control: the same world reads a boundary after the group ends.

    A detached cache keeps the group's token in server drain until the server
    reports no live compilation, so both movements must clear before the
    boundary is due.
    """
    world, shell, group_id = _world_with_a_cached_launch(tmp_path)
    _end_cached_group(world, shell, group_id)

    shell.on_tick(0.0)

    assert world.statistics.starts, (
        "the boundary is read once no cached group is moving or draining"
    )
    shell.close(timeout=0.1)
