"""Drive one cached group through the shell's launch and ending hooks.

Both shell suites need the same four steps, and a group left half-finished
would make the next read fail for a reason the test did not mean to introduce.
"""

import typing as typ

from outcome_shell_test_support import OutcomeWorld, admitted_launch

from buildlimits.effects import Exit
from buildlimits.supervisor_outcome_setup import GroupEnding


def run_cached_group(
    world: OutcomeWorld,
    shell: object,
    entry: object,
    request_id: int,
) -> str:
    """Admit, launch, finish and clear one cached group.

    The recorder's quiet gate holds a group's token in server drain until the
    server reports no live compilation, so clearing that is part of finishing a
    group rather than part of launching it.

    Returns
    -------
    str
        The group identifier admission selected.
    """
    launch = admitted_launch(world.admission, request_id)
    typ.cast("typ.Any", shell).launched(entry, launch.group_id)
    world.admission.leader_exited(launch.group_id, Exit(code=0))
    world.admission.group_emptied(launch.group_id)
    typ.cast("typ.Any", shell).emptied(
        GroupEnding(launch.group_id, is_cached=True, is_clean=True)
    )
    world.admission.server_activity(active_compiles=0)
    return launch.group_id


__all__ = ["run_cached_group"]
