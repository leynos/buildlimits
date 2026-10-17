"""Observe group drain calls while their existing owners retain exit authority."""

import functools as ft
import typing as typ

from buildlimits import cgroups

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.groups import Groups


def terminate(groups: Groups, group_id: str) -> None:
    """Schedule escalation and invoke the existing termination exactly once.

    Parameters
    ----------
    groups : Groups
        Owner of compiler scopes, deadlines and the shared drain observer.
    group_id : str
        Internal group identity, never passed into metric fields.
    """
    running = groups.running.get(group_id)
    if running is None:
        return
    if running.kill_at is None:
        running.kill_at = groups.clock() + groups.settings.cancel_grace_seconds
    groups.drain_metrics.call(
        "terminate", ft.partial(cgroups.terminate, running.scope_dir, force=False)
    )


def advance(groups: Groups, draining: cabc.Container[str]) -> list[str]:
    """Escalate due groups and retain tokens until a scope is verified empty.

    Parameters
    ----------
    groups : Groups
        Existing compiler-group owner.
    draining : Container[str]
        Internal identities admission awaits, excluded from observations.

    Returns
    -------
    list[str]
        Exactly the existing verified-empty group identities.
    """
    now = groups.clock()
    emptied = []
    for running in groups.running.values():
        if running.kill_at is not None and now >= running.kill_at:
            groups.drain_metrics.call(
                "escalate", ft.partial(cgroups.terminate, running.scope_dir, force=True)
            )
            running.kill_at = None
        if running.group_id in draining and not groups.drain_metrics.call(
            "empty_check",
            ft.partial(cgroups.is_populated, running.scope_dir),
            pending=bool,
        ):
            emptied.append(running.group_id)
    return emptied
