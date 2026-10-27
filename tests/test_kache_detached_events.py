"""What kache mode must never say, meter or do about a detached cache.

A detached backend leaves work running in a server after its group's scope has
emptied, so the supervisor owes that work an accounting story: a start-up stop,
a drain token held until the server is verified empty, a recovery transaction
when the tokens starve, and operator guidance that names the service to
recover. kache runs its compiler as a child of the wrapper *inside* the group,
so every one of those has nothing to act on. An event or a sentence emitted
anyway would not be a harmless extra: it would tell an operator to go and
recover a service this supervisor does not own.

Each test therefore asks a wide aperture rather than one call -- a whole
start-up sequence, a whole group lifetime, a whole recovery tick -- because the
failure worth catching is a new detached-only path reaching kache mode, not a
single function returning the wrong value. Where a test asserts that nothing
happened, it also pins the reading that *would* have made something happen in
sccache mode, so a passing assertion cannot mean the fixture was inert.

See ``docs/adr-004-kache-cache-backend.md`` and section 6 of the issue.
"""

import contextlib
import dataclasses as dc
import typing as typ
from pathlib import Path

import pytest
from cache_recovery_helpers import (
    MACHINE_STEP_SECONDS,
    CacheState,
    gauges,
    manager_for,
)
from request_helpers import crate_pending

from buildlimits import effect_dispatch, server_drain, status, status_render
from buildlimits.admission import Admission
from buildlimits.cache_recovery import CacheRecovery
from buildlimits.config import SupervisorConfig
from buildlimits.effects import Exit, Launch, ReturnToken
from buildlimits.metrics import Metrics
from buildlimits.recovery_barrier import RecoveryBarrier
from buildlimits.sccache import ManagedSccache
from buildlimits.server_watch import ServerWatch
from buildlimits.supervisor import Supervisor, SupervisorTelemetry
from buildlimits.timing import Clock

if typ.TYPE_CHECKING:
    from conftest import RecordingSink

    from buildlimits.cache_backend import CacheBackend
    from buildlimits.metrics import MetricsPort


@dc.dataclass(slots=True)
class _KacheServer:
    """A kache backend that refuses to be treated as a managed server.

    The two detached-only questions raise rather than answer. A kache
    supervisor that asks either of them has already broken the rule these
    tests state, and failing loudly at the call site names the caller; a
    scripted value would let it pass and be caught only as a mystery later.

    Attributes
    ----------
    reachable : bool
        The health reading :meth:`is_reachable` reports.
    server_queries : int
        How many detached-only questions were asked, which must stay zero.
    health_queries : int
        How many health polls were answered, so a test can tell a probe that
        ran and found nothing from one that never ran at all.
    """

    reachable: bool = True
    server_queries: int = 0
    health_queries: int = 0

    def is_reachable(self) -> bool:
        """Report the scripted health reading, counting the poll.

        Returns
        -------
        bool
            Whether a group may be composed with this cache.
        """
        self.health_queries += 1
        return self.reachable

    def active_compiles(self) -> int:
        """Raise: the detached-work census has no meaning for an in-scope cache.

        Raises
        ------
        AssertionError
            Always, because every kache group owns all of its own compiler
            work and there is no server to census.
        """
        self.server_queries += 1
        msg = "kache mode asked how much detached work runs"
        raise AssertionError(msg)

    def accounted_drain(self) -> int:
        """Verify the server is empty before releasing its token.

        Only a backend whose work outlives the group can be accounted for, and
        kache has no such server to verify. A kache supervisor reaching here
        has a token waiting on work that cannot exist, so this fails rather
        than answering a zero that would release it.

        Raises
        ------
        AssertionError
            Always, naming the accounting a kache supervisor must not request.
        """
        self.server_queries += 1
        msg = "kache mode asked a server to account for its work"
        raise AssertionError(msg)


def _assert_not_metered(sink: RecordingSink, event: str, why: str) -> None:
    """Check a detached-only event was never emitted, for the stated reason.

    Parameters
    ----------
    sink : RecordingSink
        The records emitted during the scenario.
    event : str
        The metric event that must be absent.
    why : str
        Why this mode cannot produce the event, quoted in the failure message.
    """
    assert sink.of_event(event) == [], f"{event} was metered although {why}"


def _watch(
    server: _KacheServer, admission: Admission, metrics: MetricsPort
) -> ServerWatch:
    """Watch a kache group's own scope through a backend with no server.

    Parameters
    ----------
    server : _KacheServer
        The fake answering both ports.
    admission : Admission
        The admission state the watch reads and re-decides.
    metrics : MetricsPort
        The emitter the test inspects.

    Returns
    -------
    ServerWatch
        The watch a kache supervisor would hold.
    """
    return ServerWatch(
        typ.cast("ManagedSccache", server),
        admission,
        "kache",
        typ.cast("CacheBackend", server),
        metrics=metrics,
    )


def _machine(
    root: Path, cache_mode: str, metrics: MetricsPort, state: CacheState
) -> CacheRecovery:
    """Build a starved recovery transaction with the strongest detached proof.

    The reading is the most permissive one the automatic trigger ever acts on:
    a live-work fault with zero visible compiler roots, a token awaiting the
    server and a request queued behind it. In ``sccache`` mode that opens a
    transaction on the first tick, which is what makes the kache assertion
    mean something.

    Parameters
    ----------
    root : Path
        A disposable directory for the socket and the restart barrier.
    cache_mode : str
        The mode the transaction is built for.
    metrics : MetricsPort
        The emitter the test inspects.
    state : CacheState
        The scripted service the manager answers from.

    Returns
    -------
    CacheRecovery
        The transaction, idle and ready to be ticked.
    """
    return CacheRecovery(
        "starved",
        ManagedSccache("cache.service", root / "sccache.sock", manager_for(state)),
        root / "cache-recovery",
        counts=lambda: gauges(queued=1, server_drain=1),
        fault_kind=lambda: "live_work",
        zero_visible_roots=lambda: True,
        roots_are_visible=lambda: False,
        cache_mode=cache_mode,
        metrics=metrics,
    )


def test_kache_start_up_never_stops_a_service_it_does_not_own(
    metrics: Metrics, metric_sink, tmp_path: Path
) -> None:
    """The two start-up stops that account for a server both find nothing."""
    server = _KacheServer()
    watch = _watch(server, Admission(cached=True, cache_detached=False), metrics)
    barrier = RecoveryBarrier(tmp_path / "cache-recovery")
    timing = Clock(now=lambda: 0.0, sleep=lambda _seconds: None)

    watch.quiesce_live_cache(barrier, timing)
    watch.drain_orphans()

    assert not barrier.path.exists(), "no barrier is written for a service nobody owns"
    assert server.server_queries == 0, "start-up asked no detached-only question"
    _assert_not_metered(
        metric_sink,
        "cache_quiesce",
        "no managed service was stopped at start-up",
    )


def test_an_emptied_kache_group_emits_no_server_drain_release(
    metrics: Metrics, metric_sink
) -> None:
    """A group returns its token as it leaves, with no server to wait for.

    The leader is killed rather than finishing cleanly, which is the one case
    a detached backend treats as possibly leaving work behind. For kache the
    ending cannot matter -- its compiler is a child of the group -- so the
    token must come back regardless, and it is the ``detached`` half of the
    admission decision that the read is pinned against.
    """
    server = _KacheServer()
    admission = Admission(cached=True, cache_detached=False)
    watch = _watch(server, admission, metrics)

    admission.submit(1, cacheable=True)
    [launch] = [
        effect for effect in admission.token_arrived(b"+") if isinstance(effect, Launch)
    ]
    admission.leader_exited(launch.group_id, Exit(signal=15))
    returned = [
        effect
        for effect in admission.group_emptied(launch.group_id)
        if isinstance(effect, ReturnToken)
    ]
    drained = server_drain.drained_tokens(watch)

    assert [effect.token for effect in returned] == [b"+"], (
        "the scenario is only meaningful if the emptied group gave its token back"
    )
    assert drained == [], "nothing was waiting on a server, so nothing is released"
    assert server.server_queries == 0, "the drain loop asked no detached-only question"
    _assert_not_metered(
        metric_sink,
        "server_drain_released",
        "no token waited on server accounting",
    )


def test_a_starved_kache_pool_never_opens_a_recovery(
    metrics: Metrics, metric_sink, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Even the strongest detached proof opens nothing in kache mode."""
    kache_state = CacheState()
    machine = _machine(tmp_path / "kache", "kache", metrics, kache_state)
    for step in range(3):
        machine.tick(MACHINE_STEP_SECONDS * step)

    assert machine.describe(0.0)["phase"] == "idle", "no transaction ever opened"
    assert not (tmp_path / "kache" / "cache-recovery").exists(), (
        "the barrier belongs to a service this mode does not own"
    )
    assert kache_state.verbs == [], "the manager was never asked to stop or start"
    _assert_not_metered(
        metric_sink,
        "cache_recovery",
        "no recovery transaction opened",
    )
    err = capsys.readouterr().err
    assert not err, f"a kache tick journaled guidance it cannot honour: {err!r}"

    control_state = CacheState()
    control = _machine(tmp_path / "control", "sccache", Metrics(), control_state)
    control.tick(0.0)
    assert control.describe(0.0)["phase"] == "quiescing", (
        "the reading is trigger-worthy, so the kache assertion is not vacuous"
    )


@pytest.mark.parametrize(
    ("caching", "phrase"),
    [
        (True, "caching through kache"),
        (False, "kache unavailable, compiling directly"),
    ],
)
def test_kache_status_names_kache_and_no_managed_service(
    owned, caching: bool, phrase: str
) -> None:
    """Status describes kache in its own words, in both health states."""
    supervisor = Supervisor(
        SupervisorConfig(cache_mode="kache", kache_executable="/nonexistent/kache")
    )
    owned.enter_context(contextlib.closing(supervisor))
    supervisor.admission.cached = caching

    document = status.describe(supervisor)
    text = status_render.render(document)

    assert document["cache_mode"] == "kache", "the document names the mode it runs"
    assert phrase in text, text
    assert "sccache" not in text, (
        f"kache status named the backend it is not using: {text!r}"
    )
    assert "build-limits-recover" not in text, (
        "an operator command that refuses in this mode must not be offered"
    )


@pytest.mark.parametrize(
    ("cached", "path"), [(True, "cached"), (False, "direct_fallback")]
)
def test_an_admitted_kache_request_names_its_backend(
    owned, telemetry, cached: bool, path: str
) -> None:
    """The metric says which cache served the build, not only how it went."""
    emitter, sink = telemetry
    supervisor = Supervisor(
        SupervisorConfig(cache_mode="kache"),
        telemetry=SupervisorTelemetry(metrics=emitter),
    )
    owned.enter_context(contextlib.closing(supervisor))

    effect_dispatch.record_admitted(supervisor, crate_pending(1), cached=cached)

    [record] = sink.of_event("request_admitted")
    assert record["BUILD_LIMITS_CACHE_BACKEND"] == "kache", (
        "the backend is a field of its own, so cache_path keeps its meaning"
    )
    assert record["BUILD_LIMITS_CACHE_PATH"] == path, record


def test_a_kache_health_cycle_journals_only_kache_lines(
    metrics: Metrics, capsys: pytest.CaptureFixture[str]
) -> None:
    """A composition change is worded for kache and offers no operator command."""
    server = _KacheServer(reachable=False)
    watch = _watch(server, Admission(cached=True, cache_detached=False), metrics)

    watch.refresh_cache_mode()
    server.reachable = True
    watch.refresh_cache_mode()

    err = capsys.readouterr().err
    assert server.health_queries == 2, (
        "both journal lines came from a real poll, not from a default reading"
    )
    assert "kache is unavailable; compiling directly" in err, err
    assert "kache is caching compilations" in err, err
    for absent in ("managed sccache", "build-limits-recover", "operator action"):
        assert absent not in err, (
            f"a kache health change carried detached-only guidance: {err!r}"
        )
