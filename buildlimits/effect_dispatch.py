"""Effect handlers shared only with the supervisor event loop."""

import typing as typ

from buildlimits.admission_state import GroupState
from buildlimits.cache_backend import is_cache
from buildlimits.effects import (
    Launch,
    NotifyFinished,
    NotifyQueued,
    Refuse,
    RequestToken,
    ReturnToken,
    Shutdown,
    Terminate,
)
from buildlimits.protocol import Queued

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.request_state import Pending
    from buildlimits.supervisor import Supervisor


def effect_handlers(
    supervisor: Supervisor,
) -> dict[type, cabc.Callable[[typ.Any], None]]:
    """Map each effect type to the method that carries it out.

    Parameters
    ----------
    supervisor : Supervisor
        The shell whose collaborators execute each effect.

    Returns
    -------
    dict[type, Callable[[Any], None]]
        One handler per effect type.
    """
    return {
        RequestToken: lambda _effect: supervisor._request_token(),
        ReturnToken: lambda effect: supervisor._return_token(effect.token),
        Launch: lambda effect: supervisor._launch(
            effect.request_id, effect.group_id, cached=effect.cached
        ),
        NotifyQueued: lambda effect: supervisor.requests.notify(
            effect.request_id, Queued(effect.position)
        ),
        NotifyFinished: lambda effect: supervisor.requests.finish(
            effect.request_id, effect.exit
        ),
        Refuse: lambda effect: supervisor.requests.refuse(
            effect.request_id, effect.reason
        ),
        Terminate: lambda effect: supervisor.groups.terminate(effect.group_id),
        Shutdown: lambda _effect: supervisor._shutdown(),
    }


def record_admitted(supervisor: Supervisor, entry: Pending, *, cached: bool) -> None:
    """Observe placement before the admission reply releases the compiler.

    Parameters
    ----------
    supervisor : Supervisor
        Event-loop owner of admission gauges and cache policy.
    entry : Pending
        Request successfully placed inside its compiler group.
    cached : bool
        Whether this group uses the configured cache backend.
    """
    entry.cache_path = (
        "cached"
        if cached
        else (
            "direct_fallback"
            if is_cache(supervisor.settings.cache_mode) and entry.request.cacheable
            else "direct"
        )
    )
    supervisor._metric(
        "request_admitted",
        cache_path=entry.cache_path,
        wait_seconds=0.0 if entry.admitted is None else entry.admitted - entry.arrived,
    )


def mark_health_cancellations(supervisor: Supervisor) -> None:
    """Preserve first cancellation causes and already ended leader results.

    Parameters
    ----------
    supervisor : Supervisor
        Owner of pending requests and admission group lifecycle state.
    """
    for entry in supervisor.requests.pending.values():
        if entry.cancellation != "none":
            continue
        if entry.group_id is None:
            entry.cancellation = "health"
            continue
        group = supervisor.admission.groups.get(entry.group_id)
        if group is not None and group.state is GroupState.RUNNING:
            entry.cancellation = "health"
