"""Supervisor- and machine-level fixtures for the recovery test modules.

:class:`Harness` pairs one production :class:`~buildlimits.supervisor.Supervisor`
with the scripted cache from :mod:`cache_recovery_helpers` and a hand-driven
clock, so a test advances the transaction without waiting on the real poll
cadence. :func:`harness_for` builds one over a disposable fixture directory;
:func:`hold_drained_group` and :func:`queue_request` seed the admission state a
recovery has to preserve, and :func:`machine_for` and
:func:`scripted_machine` build the recovery transaction directly, over gauges a
test controls.

Examples
--------
>>> gauges(queued=1, server_drain=1).server_drain
1

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
from pool_helpers import FakePool
from request_helpers import crate_pending

from buildlimits import cache_recovery
from buildlimits import sccache as sccache_module
from buildlimits.admission import Counts, Group, GroupState
from buildlimits.config import SupervisorConfig
from buildlimits.supervisor import Supervisor
from buildlimits.supervisor_polling import poll_services

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from buildlimits.metrics import MetricsPort
    from buildlimits.pool import TokenPool


def _tmp_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Map the managed cache's control group into a disposable fixture.

    Returns
    -------
    Path
        The redirected control-group directory inside the fixture.
    """
    monkeypatch.setattr(
        sccache_module.cgroups,
        "directory",
        lambda path: tmp_path / str(path).lstrip("/"),
    )
    group = tmp_path / CACHE_UNIT
    group.mkdir()
    return group


@dc.dataclass(slots=True)
class Harness:
    """One supervisor, its scripted cache and a hand-driven clock."""

    supervisor: Supervisor
    cache: CacheState
    marker: Path
    now: float = 0.0

    def tick(self) -> None:
        """Run one due server poll through the production polling order."""
        self.supervisor._next_server_poll = 0.0
        poll_services(self.supervisor, self.now)

    def advance(self, seconds: float) -> None:
        """Move the hand-driven clock forward."""
        self.now += seconds

    def account(self) -> None:
        """Drive an opened transaction through its verified accounting.

        The first tick writes the barrier and the second has the stop
        accepted; the cache then goes quiet, so the third tick's accounting
        proof returns the drained tokens.
        """
        for _phase in ("quiescing", "stopping"):
            self.tick()
        self.cache.active = False
        self.tick()

    def exhaust_retries(self) -> None:
        """Tick past every bounded retry the current phase allows."""
        for _ in range(cache_recovery.RECOVERY_FAILURE_LIMIT + 2):
            self.advance(cache_recovery.RECOVERY_MAX_RETRY_SECONDS)
            self.tick()

    def described(self) -> dict[str, object]:
        """Describe the recovery transaction at the scripted time.

        Returns
        -------
        dict[str, object]
            The operator-facing phase, attempts and fault text.
        """
        return self.supervisor.recovery.describe(self.now)

    @property
    def pool(self) -> FakePool:
        """Read the scripted token pool."""
        return typ.cast("FakePool", self.supervisor.pool)


def harness_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "starved",
) -> Harness:
    """Build a supervisor whose cache state is fully scripted.

    Parameters
    ----------
    tmp_path : Path
        The disposable fixture directory holding the marker and control group.
    monkeypatch : pytest.MonkeyPatch
        Fixture used to redirect the cache's control-group lookup.
    mode : str
        The configured recovery policy.

    Returns
    -------
    Harness
        The supervisor, its scripted cache and a hand-driven clock.
    """
    group = _tmp_group(tmp_path, monkeypatch)
    state = CacheState()
    state.record(group)
    marker = tmp_path / "cache-recovery"
    state.marker = marker
    settings = SupervisorConfig(
        cache_mode="sccache",
        cache_recovery=mode,
        sccache_unit=CACHE_UNIT,
        sccache_socket=tmp_path / "sccache.sock",
        recovery_marker=marker,
        # Binding a requester socket unlinks whatever already sits at the
        # configured path, so the fixture directory is the only safe place for
        # it; the default path belongs to the operator's live supervisor.
        socket_path=tmp_path / "supervisor.sock",
    )
    supervisor = Supervisor(settings, manager_for(state))
    supervisor.pool = typ.cast(
        "TokenPool", FakePool(wake_fd=-1, capacity=settings.slots)
    )
    supervisor._next_health_poll = float("inf")
    supervisor._next_cache_poll = float("inf")
    harness = Harness(supervisor, state, marker)
    supervisor.clock = lambda: harness.now
    return harness


def opened_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    refuse: str | None = None,
    queued: tuple[int, ...] = (),
) -> Harness:
    """Open a manual transaction over one drained cached group.

    Parameters
    ----------
    tmp_path : Path
        The disposable fixture directory holding the marker and control group.
    monkeypatch : pytest.MonkeyPatch
        Fixture used to redirect the cache's control-group lookup.
    refuse : str or None
        The control verb the scripted manager refuses, or None for none.
    queued : tuple[int, ...]
        Requests left waiting before the transaction opens, in queue order.

    Returns
    -------
    Harness
        A harness whose transaction is open but has not yet ticked.
    """
    harness = harness_for(tmp_path, monkeypatch)
    hold_drained_group(harness.supervisor)
    for request_id in queued:
        queue_request(harness.supervisor, request_id)
    harness.cache.refuse = refuse
    harness.supervisor.recovery.request("manual", harness.now)
    return harness


def hold_drained_group(supervisor: Supervisor, group_id: str = "g1") -> None:
    """Account for one cached group that is waiting on the server.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor whose admission state receives the group.
    group_id : str
        Identifier of the retained group.
    """
    supervisor.admission.groups[group_id] = Group(
        group_id, 1, b"+", cached=True, state=GroupState.SERVER_DRAIN
    )


def hold_drain_beside_running_group(supervisor: Supervisor) -> None:
    """Retain one drained group while a cached group is still running."""
    hold_drained_group(supervisor)
    supervisor.admission.groups["g2"] = Group("g2", 2, b"+", cached=True)


def queue_request(supervisor: Supervisor, request_id: int = 2) -> None:
    """Leave one unrelated requester waiting for a token.

    Parameters
    ----------
    supervisor : Supervisor
        The supervisor whose admission state receives the request.
    request_id : int
        Identifier of the queued request.
    """
    supervisor.admission.queue.append(request_id)
    supervisor.admission.cacheable[request_id] = True
    supervisor.requests.pending[request_id] = crate_pending(request_id)


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
