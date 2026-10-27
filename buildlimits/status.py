"""The status document that the supervisor reports about itself.

Guildmaster reports no free, held or waiting counts, and probing its pool by
reading it would take a token. The status therefore comes from the
supervisor's own state: capacity, held tokens, waiting requests, running and
draining groups, and whether new groups are being cached. :func:`describe`
builds that document inside the supervisor. :mod:`buildlimits.status_client`
fetches it, :mod:`buildlimits.status_render` presents it, and
:mod:`buildlimits.status_cli` is the operator command.
"""

import resource
import typing as typ

from buildlimits import descriptors, kache_guard

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.pending_request import Pending
    from buildlimits.supervisor import Supervisor


def describe(supervisor: Supervisor) -> dict[str, object]:
    """Describe a supervisor's current state.

    Parameters
    ----------
    supervisor : Supervisor
        The running supervisor.

    Returns
    -------
    dict[str, object]
        A JSON-compatible document with queue budgets and live descriptor gauges.
        Failed enumeration reports an unknown count and its errno; unlimited
        limits use ``-1``. Compatibility limit aliases share the same sample.
    """
    admission = supervisor.admission
    now = supervisor.clock()
    groups: list[dict[str, object]] = []
    for group in admission.groups.values():
        entry = supervisor.requests.pending.get(group.request_id)
        metadata = supervisor.requests.completed_metadata.get(group.group_id, {})
        admitted = entry.admitted if entry else metadata.get("admitted")
        if not isinstance(admitted, (float, int)):
            admitted = None
        groups.append({
            "group": group.group_id,
            "state": group.state.value,
            "cached": group.cached,
            "metadata_available": entry is not None or bool(metadata),
            "label": entry.request.label if entry else metadata.get("label"),
            "directory": entry.request.cwd if entry else metadata.get("directory"),
            "running_seconds": None if admitted is None else round(now - admitted, 1),
        })
    settings = supervisor.settings
    strays = _stray_kache_processes(supervisor)
    return {
        "capacity": settings.slots,
        "held": admission.tokens_held(),
        "healthy": admission.healthy,
        "restarting": admission.winding_down,
        "health_reason": supervisor.health_reason,
        "drain_fault": supervisor.server.drain_fault,
        "drain_fault_kind": supervisor.server.drain_fault_kind,
        "drain_attempts": supervisor.server.drain_attempts,
        "recovery_action": supervisor.server.recovery_action
        or supervisor.loop.recovery_action,
        # True only for the cache-recovery text that tells the operator the
        # command is a fallback; the loop's own action never is.
        "recovery_action_is_fallback": bool(
            supervisor.server.recovery_action
            and supervisor.server.recovery_action_is_fallback
        ),
        "device": str(settings.device),
        "cache_mode": settings.cache_mode,
        "caching": admission.cached,
        # A fault, not a token holder: a stray cache process outside every
        # admitted scope is reported and nothing else. Always present, so a
        # reader can distinguish "no strays" from "not guarded".
        "stray_kache_processes": strays,
        # Recovery never takes a waiting request's place, so the queue is
        # reported exactly as admission holds it.
        "cache_recovery": supervisor.recovery.describe(now),
        "queued": queued_requests(supervisor.requests.pending, admission.queue, now),
        "groups": groups,
        "descriptors": _descriptor_metrics(supervisor),
    }


def _stray_kache_processes(supervisor: Supervisor) -> list[int]:
    """Read the current stray cache processes, when this mode can have any.

    Only an in-scope cache runs inside an admitted group, so only it can leave
    a process behind there; every other mode reports an empty list without
    touching ``/proc`` or the slice. The read is taken here rather than reused
    from the last poll because status is an operator asking *now*.

    Returns
    -------
    list[int]
        The uncovered kache process identifiers, empty when the mode is not
        guarded or the guard found none.
    """
    groups = supervisor.groups
    if not kache_guard.guarded(groups):
        return []
    groups.guard.observe(groups.slice_dir, kache_guard.admitted_scopes(groups))
    return groups.guard.describe()


def _descriptor_metrics(supervisor: Supervisor) -> dict[str, int | None]:
    """Sample process headroom without making failed enumeration fatal.

    Returns
    -------
    dict[str, int | None]
        Fixed gauges; failed enumeration leaves usage unknown with its errno.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        count = len(descriptors.open_descriptors_except(()))
    except OSError as error:
        count, sampling_errno = None, error.errno
    else:
        sampling_errno = None
    return supervisor.requests.descriptors.describe(
        supervisor.requests.retained,
        sample={
            "soft": soft,
            "hard": hard,
            "open": count,
            "soft_limit": soft,
            "hard_limit": hard,
            "sampling_errno": sampling_errno,
        },
    )


def queued_requests(
    pending: cabc.Mapping[int, Pending], queue: cabc.Iterable[int], now: float
) -> list[dict[str, object]]:
    """Describe recorded waiting requests in admission queue order.

    Parameters
    ----------
    pending : Mapping[int, Pending]
        Existing requester lifecycle records, read without changing ownership.
    queue : Iterable[int]
        Admission queue identifiers in their existing order.
    now : float
        Monotonic timestamp for elapsed waiting time.

    Returns
    -------
    list[dict[str, object]]
        Existing status fields for queued identifiers which are still recorded.
    """
    return [
        {
            "request": request_id,
            "label": pending[request_id].request.label,
            "directory": pending[request_id].request.cwd,
            "waiting_seconds": round(now - pending[request_id].arrived, 1),
        }
        for request_id in queue
        if request_id in pending
    ]
