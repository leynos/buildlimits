"""Recovery-machine builders for tests that drive the transaction directly.

:func:`machine_for` and :func:`metered_machine` build the recovery transaction
over scripted gauges and a scripted manager; :func:`scripted_machine` also lets
a test rewrite those gauges mid-transaction, and :func:`tick_phase` ticks one
and reads the phase back. None of them owns a descriptor. The supervisor-level
harness lives in :mod:`cache_recovery_harness`.
"""

import dataclasses as dc
import typing as typ

from cache_recovery_helpers import (
    CACHE_UNIT,
    LIVE_WORK_ZERO_ROOTS,
    CacheState,
    DrainReading,
    gauges,
    manager_for,
)

from buildlimits import cache_recovery
from buildlimits import sccache as sccache_module

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.admission import Counts
    from buildlimits.metrics import MetricsPort


def machine_for(
    tmp_path: Path,
    *,
    mode: str = "starved",
    snapshot: Counts | None = None,
    reading: DrainReading = LIVE_WORK_ZERO_ROOTS,
) -> tuple[cache_recovery.CacheRecovery, CacheState]:
    """Build the recovery machine over scripted gauges and a scripted manager.

    Parameters
    ----------
    tmp_path : Path
        The disposable fixture directory holding the marker and control group.
    mode : str
        The configured recovery policy.
    snapshot : Counts or None
        The gauge snapshot every tick reads, or None for an idle snapshot.
    reading : DrainReading
        The single drain reading the watch's fault kind and zero-root proof
        are both answered from, so the two cannot disagree.

    Returns
    -------
    tuple[cache_recovery.CacheRecovery, CacheState]
        The machine and the scripted cache it controls.
    """
    state = CacheState()
    state.record(tmp_path / CACHE_UNIT)
    state.marker = tmp_path / "cache-recovery"
    machine = cache_recovery.CacheRecovery(
        mode,
        sccache_module.ManagedSccache(
            CACHE_UNIT, tmp_path / "sccache.sock", manager_for(state)
        ),
        state.marker,
        counts=lambda: snapshot if snapshot is not None else gauges(),
        fault_kind=lambda: reading.kind,
        zero_visible_roots=lambda: reading.proves_zero_roots,
        roots_are_visible=lambda: reading.shows_visible_roots,
    )
    return machine, state


def metered_machine(
    tmp_path: Path, metrics: MetricsPort, *, snapshot: list[Counts]
) -> tuple[cache_recovery.CacheRecovery, CacheState]:
    """Build a recovery machine over one mutable gauge cell and an emitter.

    Parameters
    ----------
    tmp_path : Path
        The disposable fixture directory holding the marker and control group.
    metrics : MetricsPort
        The emitter the machine records its transaction through.
    snapshot : list[Counts]
        A one-element cell every tick reads; replacing the cell rather than
        rebuilding the machine keeps the port the one the machine reads.

    Returns
    -------
    tuple[cache_recovery.CacheRecovery, CacheState]
        The machine and the scripted cache it controls.
    """
    machine, state = machine_for(tmp_path)
    machine.counts = lambda: snapshot[0]
    machine.metrics = metrics
    return machine, state


@dc.dataclass(slots=True)
class ScriptedCache:
    """A recovery machine over a scripted cache and a mutable gauge source.

    Attributes
    ----------
    machine : cache_recovery.CacheRecovery
        The transaction machine under test.
    cache : CacheState
        The scripted managed cache it controls.
    snapshot : list[Counts]
        The gauge snapshot every tick reads; tests mutate element zero to
        advance the accounting proof without building real compiler groups.
    roots : list[int or None]
        The visible compiler roots the zero-root proof reports; tests mutate
        element zero to withdraw a proof mid-transaction.
    kind : list[str or None]
        The drain fault the server watch reports; tests mutate element zero to
        fault the manager transport mid-transaction. It is separate from
        ``roots`` because the live watch records a root count only for the
        live-work fault, so the two cannot be read as one variable.
    """

    machine: cache_recovery.CacheRecovery
    cache: CacheState
    snapshot: list[Counts]
    roots: list[int | None]
    kind: list[str | None]


def scripted_machine(
    tmp_path: Path,
    snapshot: list[Counts],
    *,
    roots: int | None = 0,
    kind: str | None = "live_work",
) -> ScriptedCache:
    """Build a recovery machine whose gauges a test can rewrite mid-transaction.

    Parameters
    ----------
    tmp_path : Path
        The disposable fixture directory holding the marker and control group.
    snapshot : list[Counts]
        A one-element list holding the gauge snapshot every tick reads, so a
        test can advance the accounting proof without building real groups.
    roots : int or None
        Visible compiler roots the zero-root proof reports, or None for a
        count the diagnostic could not establish.
    kind : str or None
        The drain fault the server watch reports, or None for a clean probe.

    Returns
    -------
    ScriptedCache
        The machine, the scripted cache and the mutable gauge source.
    """
    state = CacheState()
    state.record(tmp_path / CACHE_UNIT)
    state.marker = tmp_path / "cache-recovery"
    roots_cell = [roots]
    kind_cell = [kind]
    machine = cache_recovery.CacheRecovery(
        "starved",
        sccache_module.ManagedSccache(
            CACHE_UNIT, tmp_path / "sccache.sock", manager_for(state)
        ),
        state.marker,
        counts=lambda: snapshot[0],
        fault_kind=lambda: kind_cell[0],
        zero_visible_roots=lambda: roots_cell[0] == 0,
        roots_are_visible=lambda: (
            (roots_cell[0] or 0) > 0 and kind_cell[0] == "live_work"
        ),
    )
    return ScriptedCache(machine, state, snapshot, roots_cell, kind_cell)


def tick_phase(cache: ScriptedCache, now: float) -> object:
    """Tick a scripted machine at one instant and read back its phase.

    Parameters
    ----------
    cache : ScriptedCache
        The machine under test.
    now : float
        The instant of the tick.

    Returns
    -------
    object
        The phase the transaction document reports after the tick.
    """
    cache.machine.tick(now)
    return cache.machine.describe(now)["phase"]
