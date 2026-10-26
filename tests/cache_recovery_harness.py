"""Supervisor- and machine-level fixtures for the recovery test modules.

:class:`Harness` pairs one production :class:`~buildlimits.supervisor.Supervisor`
with the scripted cache from :mod:`cache_recovery_helpers` and a hand-driven
clock, so a test advances the transaction without waiting on the real poll
cadence. :func:`harness_for` builds one over a disposable fixture directory;
:func:`hold_drained_group` and :func:`queue_request` seed the admission state a
recovery has to preserve. The machine-only builders are in
:mod:`cache_recovery_machines`.
"""

import dataclasses as dc
import typing as typ

from cache_recovery_helpers import (
    CACHE_UNIT,
    CacheState,
    manager_for,
)
from pool_helpers import FakePool
from request_helpers import crate_pending

from buildlimits import cache_recovery, sccache_cgroup
from buildlimits.admission import Group, GroupState
from buildlimits.config import SupervisorConfig
from buildlimits.supervisor import Supervisor
from buildlimits.supervisor_polling import poll_services

if typ.TYPE_CHECKING:
    import contextlib
    from pathlib import Path

    import pytest

    from buildlimits.pool import TokenPool


def _tmp_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Map the managed cache's control group into a disposable fixture.

    Returns
    -------
    Path
        The redirected control-group directory inside the fixture.
    """
    monkeypatch.setattr(
        sccache_cgroup.cgroups,
        "directory",
        lambda path: tmp_path / str(path).lstrip("/"),
    )
    group = tmp_path / CACHE_UNIT
    group.mkdir()
    return group


@dc.dataclass(slots=True)
class Harness:
    """One supervisor, its scripted cache and a hand-driven clock.

    The supervisor's selector owns an epoll descriptor that only a close
    releases, so a harness is a context manager whose exit closes the
    supervisor. Tests obtain harnesses from the ``recovery_harnesses`` fixture,
    which closes each at teardown.
    """

    supervisor: Supervisor
    cache: CacheState
    marker: Path
    now: float = 0.0

    def __enter__(self) -> Harness:
        """Hand the harness to a ``with`` block that closes it on exit.

        Returns
        -------
        Harness
            This harness.
        """
        return self

    def __exit__(self, *_exc: object) -> None:
        """Release the supervisor's descriptors, whether or not the block raised."""
        self.supervisor.close()

    def tick(self) -> None:
        """Run one due server poll through the production polling order."""
        self.supervisor._next_server_poll = 0.0
        poll_services(self.supervisor, self.now)

    def advance(self, seconds: float) -> None:
        """Move the hand-driven clock forward."""
        self.now += seconds

    def account(self) -> None:
        """Drive an opened transaction through its verified accounting.

        The first tick writes the barrier, the second has the stop accepted,
        and the third's accounting proof returns the drained tokens.
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

    The caller owns the result and must close it: use ``with`` or the
    ``recovery_harnesses`` fixture, never a bare call.

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
    harness: Harness,
    *,
    refuse: str | None = None,
    queued: tuple[int, ...] = (),
) -> Harness:
    """Open a manual transaction over one drained cached group.

    Parameters
    ----------
    harness : Harness
        A freshly built harness, which the caller keeps ownership of.
    refuse : str or None
        The control verb the scripted manager refuses, or None for none.
    queued : tuple[int, ...]
        Requests left waiting before the transaction opens, in queue order.

    Returns
    -------
    Harness
        The same harness, with its transaction open but not yet ticked.
    """
    hold_drained_group(harness.supervisor)
    for request_id in queued:
        queue_request(harness.supervisor, request_id)
    harness.cache.refuse = refuse
    harness.supervisor.recovery.request("manual", harness.now)
    return harness


class HarnessFactory:
    """Build recovery harnesses that close themselves at the end of a test.

    Obtain one from the ``recovery_harnesses`` fixture. Every harness it builds
    is entered into one :class:`contextlib.ExitStack`, which the fixture
    unwinds at teardown even when the test failed.

    Parameters
    ----------
    tmp_path : Path
        The disposable fixture directory holding the marker and control group.
    monkeypatch : pytest.MonkeyPatch
        Fixture used to redirect the cache's control-group lookup.
    owned : contextlib.ExitStack
        The stack that closes every harness built here.
    """

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        owned: contextlib.ExitStack,
    ) -> None:
        """Remember the fixtures every harness is built from."""
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch
        self._owned = owned

    def build(self, *, mode: str = "starved") -> Harness:
        """Build an owned harness over a fully scripted cache.

        Parameters
        ----------
        mode : str
            The configured recovery policy.

        Returns
        -------
        Harness
            A harness that is closed when the test ends.
        """
        return self._owned.enter_context(
            harness_for(self._tmp_path, self._monkeypatch, mode=mode)
        )

    def opened(
        self, *, refuse: str | None = None, queued: tuple[int, ...] = ()
    ) -> Harness:
        """Build an owned harness whose manual transaction is already open.

        Parameters
        ----------
        refuse : str or None
            The control verb the scripted manager refuses, or None for none.
        queued : tuple[int, ...]
            Requests left waiting before the transaction opens.

        Returns
        -------
        Harness
            A harness that is closed when the test ends.
        """
        return opened_recovery(self.build(), refuse=refuse, queued=queued)


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
