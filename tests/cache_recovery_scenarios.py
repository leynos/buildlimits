"""Shared scenes for the stale-recovery tests: a busy host and its supervisor.

Both the stale-opening tests and the roots-deadline tests start from the same
host (one retained token beside cached compiles) and the same production
wiring, so the scenes live here once. It is an unnumbered helper module, named
like its siblings so pytest collects nothing from it.
"""

import typing as typ

from cache_recovery_harness import (
    harness_for,
    hold_drain_beside_running_group,
    queue_request,
)

from buildlimits.admission_state import Counts

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from cache_recovery_harness import Harness


def retained_counts(*, cached_moving: int) -> Counts:
    """Name a host holding one retained token beside some cached compiles.

    Parameters
    ----------
    cached_moving : int
        Cached groups still running; three is a busy host, where only the
        drain's age can open a transaction, and zero a quiet one, where the
        stop is no longer blocked by cached work.

    Returns
    -------
    Counts
        The snapshot.
    """
    return Counts(9, 3 + min(cached_moving, 1), cached_moving, 0, 1, 4, cached_moving)


def busy_supervisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """Build the production supervisor over a busy host with a visible root.

    A cached group keeps running and a visible root defeats the prompt proof,
    so only the retained age can open the transaction. The probe's root count
    has no public setter; a visible root is what defeats the prompt proof.

    Returns
    -------
    Harness
        The harness, before its first tick.
    """
    harness = harness_for(tmp_path, monkeypatch, mode="starved")
    supervisor = harness.supervisor
    hold_drain_beside_running_group(supervisor)
    queue_request(supervisor)
    supervisor.server.drain_fault_kind = "live_work"
    monkeypatch.setattr(supervisor.server, "_fault_roots", 2)
    return harness
