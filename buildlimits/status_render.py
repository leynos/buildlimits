"""Present the supervisor's status document to people.

:func:`render` turns the document that :func:`buildlimits.status.describe`
builds into the short summary ``build-limits-status`` prints. It is a pure
function of the document, so every variant can be pinned by a snapshot.
"""

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def render(state: cabc.Mapping[str, object]) -> str:
    """Render a status document for people.

    Parameters
    ----------
    state : Mapping[str, object]
        The status document.

    Returns
    -------
    str
        A short multi-line summary.

    Examples
    --------
    >>> state = {"capacity": 2, "held": 1, "healthy": True, "caching": True}
    >>> print(render(state))
    capacity 2, held 1, healthy, caching through sccache
    no requests waiting
    no compiler groups
    """
    health = "healthy" if state.get("healthy") else "UNHEALTHY"
    summary = (
        f"capacity {state.get('capacity')}, held {state.get('held')}, {health}, "
        f"{_cache_description(state)}"
    )
    queued = _entries(state, "queued")
    groups = _entries(state, "groups")
    return "\n".join([
        summary,
        *_descriptor_lines(state),
        *[
            f"{label}: {state[key]}"
            for key, label in (
                ("health_reason", "unhealthy reason"),
                ("drain_fault_kind", "drain fault kind"),
                ("drain_fault", "drain check failed; tokens retained"),
                ("recovery_action", "required recovery action"),
            )
            if state.get(key)
        ],
        *_recovery_lines(state),
        *([_waiting_line(item) for item in queued] or ["no requests waiting"]),
        *([_group_line(item) for item in groups] or ["no compiler groups"]),
    ])


def _section(
    state: cabc.Mapping[str, object], key: str
) -> cabc.Mapping[str, object] | None:
    """Read an optional nested section of the status document.

    Returns
    -------
    Mapping[str, object] | None
        The section, or None when a legacy document omits it or it is not a
        mapping.
    """
    section = state.get(key)
    return (
        typ.cast("cabc.Mapping[str, object]", section)
        if isinstance(section, dict)
        else None
    )


def _descriptor_lines(state: cabc.Mapping[str, object]) -> list[str]:
    """Render fixed descriptor gauges when a supervisor supplies them.

    Returns
    -------
    list[str]
        One gauge summary, or no lines for a legacy document.
    """
    gauges = _section(state, "descriptors")
    if gauges is None:
        return []
    count = gauges.get("open")
    usage = (
        str(count)
        if count is not None
        else f"unavailable (errno {gauges.get('sampling_errno')})"
    )
    return [
        (
            f"open descriptors {usage}, "
            f"soft limit {_limit_text(gauges.get('soft_limit'))}, "
            f"hard limit {_limit_text(gauges.get('hard_limit'))}"
        )
    ]


def _limit_text(value: object) -> str:
    """Name the unlimited sentinel without hiding finite limits.

    Returns
    -------
    str
        The numeric value, or ``unlimited`` for the sentinel.
    """
    return "unlimited" if value == -1 else str(value)


def _cache_description(state: cabc.Mapping[str, object]) -> str:
    """Say how new groups are composed with sccache.

    Returns
    -------
    str
        A short phrase for the summary line.
    """
    if state.get("caching"):
        return "caching through sccache"
    if state.get("cache_mode") == "sccache":
        return "sccache unreachable, compiling directly"
    return "compiling directly"


def _recovery_lines(state: cabc.Mapping[str, object]) -> list[str]:
    """Describe a running cache-recovery transaction, and its last fault.

    Returns
    -------
    list[str]
        The phase and its retry, the fault that stopped its last action, and
        any operator action still needed. A completed transaction adds the
        time before the automatic trigger may act again and the fault a
        refused restart ended on; a clean one adds nothing once its rest has
        passed, so a healthy document renders as it always did.
    """
    transaction = _section(state, "cache_recovery")
    if transaction is None:
        return []
    phase = transaction.get("phase")
    if phase == "idle":
        # A resting trigger is otherwise invisible: retained tokens beside an
        # idle transaction would read as a trigger that never fires.
        cooling = transaction.get("cooling_for")
        is_resting = isinstance(cooling, (int, float)) and cooling > 0
        lines = (
            [f"cache recovery idle: automatic recovery may start in {cooling}s"]
            if is_resting
            else []
        )
    else:
        lines = [
            (
                f"cache recovery {phase}: next attempt in "
                f"{transaction.get('retry_in')}s, "
                f"attempts {transaction.get('attempts')}"
            )
        ]
    # A clean ending clears its fault, so an idle transaction that still holds
    # one ended with its restart refused, which the operator must see.
    last_fault = transaction.get("last_fault")
    if last_fault:
        lines.append(f"cache recovery last fault: {last_fault}")
    return lines


def _entries(state: cabc.Mapping[str, object], key: str) -> list[dict[str, object]]:
    """Read one list from the status document.

    Returns
    -------
    list[dict[str, object]]
        The entries, or an empty list when the key is absent.
    """
    return typ.cast("list[dict[str, object]]", state.get(key) or [])


def _waiting_line(item: dict[str, object]) -> str:
    """Describe one waiting request.

    Returns
    -------
    str
        The line.
    """
    return (
        f"waiting {item.get('waiting_seconds')}s: {item.get('label')} in "
        f"{item.get('directory')}"
    )


def _group_line(item: dict[str, object]) -> str:
    """Describe one compiler group.

    Returns
    -------
    str
        The line.
    """
    cached = " (cached)" if item.get("cached") else ""
    label, directory = item.get("label"), item.get("directory")
    if label is None or directory is None:
        description = "request metadata unavailable; retaining compiler token"
    else:
        duration = item.get("running_seconds")
        elapsed = "duration unavailable" if duration is None else f"{duration}s"
        description = f"{label} in {directory}, {elapsed}"
    return f"{item.get('group')} {item.get('state')}{cached}: {description}"
