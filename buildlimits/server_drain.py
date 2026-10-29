"""Decide whether a server-drain token may return.

:class:`buildlimits.server_watch.ServerWatch` holds the readings; this module
owns what they mean. Keeping the decision apart from the probe leaves the watch
a view of the configured cache rather than a catalogue of release policy, and
puts that policy next to the guidance it journals, as ``drain_loop`` and
``drain_metrics`` already do for their own halves.

Every function takes the watch it decides for. They read and write the fields
:class:`~buildlimits.server_watch.ServerWatch` declares, and none of them only
forwards its arguments to another function, so the split introduces no wrapper
for DF12's trivial-wrapper check to reject.
"""

import contextlib
import typing as typ

from buildlimits.admission import GroupState, ReturnToken
from buildlimits.cache_backend import CacheError, LiveCacheWorkError
from buildlimits.cache_modes import mode_name
from buildlimits.drain_guidance import (
    FAULT_PREFIXES,
    MANDATORY_ACTIONS,
    PENDING_LIVE_WORK_ACTION,
    classify,
    journal_prefix,
)
from buildlimits.journal import log
from buildlimits.metrics import gauge_fields

if typ.TYPE_CHECKING:
    from buildlimits.admission import Effect
    from buildlimits.server_watch import ServerWatch


DRAIN_FAILURE_LIMIT = 8
DRAIN_RETRY_SECONDS = 30.0


def drained_tokens(watch: ServerWatch) -> list[Effect]:
    """Decide which server-drain tokens the server's load releases.

    Returns
    -------
    list[Effect]
        The returns admission decides on, or nothing while no token waits
        or the load cannot be read.
    """
    waiting = any(
        group.state is GroupState.SERVER_DRAIN
        for group in watch.admission.groups.values()
    )
    if not waiting:
        _clear_drain_fault(watch)
        return []
    if watch.clock() < watch._next_drain:
        return []
    try:
        active = watch.drain_metrics.call(
            "accounted_cache", watch.server.accounted_drain
        )
    except CacheError as error:
        _load_state(watch, unknown=True)
        _record_drain_fault(watch, error)
        return []
    return _release_accounted(watch, active)


def _release_accounted(watch: ServerWatch, active: int) -> list[Effect]:
    """Release what a verified stopped-and-empty cache leaves unaccounted.

    Only reached once the manager has proved the cache stopped and empty,
    so this is where the earlier fault is retired and the tokens the server
    no longer covers go back. The composition flag is whatever
    :meth:`~buildlimits.server_watch.ServerWatch.refresh_cache_mode` last
    decided from a real probe; this method changes no part of it.

    Returns
    -------
    list[Effect]
        The returns admission decides on for ``active`` live compilations.
    """
    if watch.drain_fault is not None:
        with contextlib.suppress(OSError):
            log(
                f"{mode_name(watch.cache_mode)} service verified stopped "
                "and empty; drain tokens may return"
            )
    _clear_drain_fault(watch)
    _load_state(watch, unknown=False)
    effects = watch.admission.server_activity(active)
    released = sum(isinstance(effect, ReturnToken) for effect in effects)
    if released:
        watch.metrics.emit(
            "server_drain_released",
            **gauge_fields(watch.admission.counts()),
            released=released,
        )
    return effects


def _record_drain_fault(watch: ServerWatch, error: CacheError) -> None:
    """Keep unknown work accounted for and deduplicate drain diagnostics."""
    kind = classify(error)
    retained = kind == "live_work"
    detail = ascii(str(error)[:400])[1:-1]
    fault = f"retained detached work: {detail}" if retained else detail
    watch.drain_attempts += 1
    changed = fault != watch.drain_fault or kind != watch.drain_fault_kind
    watch.drain_fault = fault
    watch.drain_fault_kind = kind
    match error:
        case LiveCacheWorkError(roots=roots):
            watch._fault_roots = roots
        case _:
            watch._fault_roots = None
    if changed:
        with contextlib.suppress(OSError):
            log(f"{FAULT_PREFIXES[kind]}{fault}")
    if watch.drain_attempts >= DRAIN_FAILURE_LIMIT:
        watch._next_drain = watch.clock() + DRAIN_RETRY_SECONDS
        watch._action_kind = kind
        _journal_recovery_action(watch)


def recovery_action(watch: ServerWatch) -> str | None:
    """Return the guidance for the current fault, once its retries are spent.

    Read live rather than stored, because whether the operator is needed
    changes while the fault stands: a live-work fault is the supervisor's
    to recover while its own recovery is pending, and the operator's once
    that cannot happen.

    Returns
    -------
    str | None
        The guidance, or ``None`` before the retries are exhausted.
    """
    if watch._action_kind is None:
        return None
    if recovery_action_is_fallback(watch):
        return PENDING_LIVE_WORK_ACTION
    return MANDATORY_ACTIONS[watch._action_kind]


def recovery_action_is_fallback(watch: ServerWatch) -> bool:
    """Whether the operator command is only a fallback for the supervisor.

    Returns
    -------
    bool
        True only for a live-work fault whose automatic recovery is
        pending. A manager or stopped-populated fault, the manual policy
        and exhausted attempts all answer False.
    """
    return watch._action_kind == "live_work" and watch.automatic_recovery_pending()


def _journal_recovery_action(watch: ServerWatch) -> None:
    """Journal the guidance once per wording, not once per retry."""
    action = recovery_action(watch)
    if action is None or action == watch._journaled_action:
        return
    watch._journaled_action = action
    prefix = journal_prefix(is_fallback=recovery_action_is_fallback(watch))
    with contextlib.suppress(OSError):
        log(f"{prefix}: {action}")


def zero_visible_roots(watch: ServerWatch) -> bool:
    """Whether the current live-work fault proved zero visible compiler roots.

    Returns
    -------
    bool
        True only for a live-work fault whose root count was established
        as zero. An unknown count and a manager fault both answer False,
        so the automatic trigger cannot act on an unproven reading.
    """
    return watch.drain_fault_kind == "live_work" and watch._fault_roots == 0


def roots_are_visible(watch: ServerWatch) -> bool:
    """Whether the current live-work fault established a visible root.

    Returns
    -------
    bool
        True only for a live-work fault whose root count was established
        as positive; an unknown count answers False, as it does for the
        zero-root proof, so neither proof is ever guessed.
    """
    roots = watch._fault_roots
    return watch.drain_fault_kind == "live_work" and roots is not None and roots > 0


def _clear_drain_fault(watch: ServerWatch) -> None:
    """End the query fault only after a successful check or no waiting work."""
    watch.drain_fault = None
    watch.drain_fault_kind = None
    watch.drain_attempts = 0
    watch._action_kind = None
    watch._journaled_action = None
    watch._next_drain = 0.0
    watch._fault_roots = None


def _load_state(watch: ServerWatch, *, unknown: bool) -> None:
    """Emit one transition per change in server-load readability."""
    if unknown != watch._load_unknown:
        watch._load_unknown = unknown
        watch.metrics.emit(
            "cache_load_unknown",
            **gauge_fields(watch.admission.counts()),
            load_unknown=unknown,
        )
