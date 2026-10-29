"""Tests for the supervisor's decisions about the managed sccache server."""

import dataclasses as dc
import subprocess
import typing as typ
from pathlib import Path

import pytest
from cache_recovery_helpers import fixture_cgroup
from test_ports import FakeRunner

from buildlimits.admission import Admission, GroupState, ReturnToken
from buildlimits.sccache import ManagedSccache, SccacheError
from buildlimits.server_watch import ServerWatch
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.cache_backend import CacheBackend


@dc.dataclass(slots=True)
class FakeServer:
    """A managed server whose answers are scripted."""

    load: int | SccacheError = 0
    reachable: bool | SccacheError = True
    unit: str = "s.service"
    stopped: bool = False
    drain_queries: int = 0

    def active_compiles(self) -> int:
        """Return or raise the scripted load."""
        if isinstance(self.load, SccacheError):
            raise self.load
        return self.load

    def accounted_drain(self) -> int:
        """Verify the scripted stopped state before returning an accounted zero.

        Returns
        -------
        int
            Zero when stopped and empty.

        Raises
        ------
        SccacheError
            If the manager cannot be queried or the service remains live.
        """
        self.drain_queries += 1
        if isinstance(self.load, SccacheError):
            raise self.load
        if not self.stopped or self.load:
            msg = "live or nonempty cache requires operator accounting"
            raise SccacheError(msg)
        return 0

    def is_reachable(self) -> bool:
        """Return or raise the scripted reachability."""
        if isinstance(self.reachable, SccacheError):
            raise self.reachable
        return self.reachable


def _watch(
    server: FakeServer,
    admission: Admission | None = None,
    mode: str = "sccache",
    backend: FakeServer | None = None,
) -> ServerWatch:
    """Watch a fake server for an admission state.

    ``server`` is the detached adapter the drain paths query. ``backend`` is the
    health port :meth:`ServerWatch.refresh_cache_mode` probes; it defaults to
    the same fake for the cases that do not distinguish the two.
    """
    return ServerWatch(
        typ.cast("ManagedSccache", server),
        admission or Admission(cached=False),
        mode,
        typ.cast("CacheBackend", backend if backend is not None else server),
    )


def _draining(token: bytes) -> Admission:
    """Build an admission state with one group waiting on the server's load."""
    admission = Admission(cached=True)
    admission.submit(1, cacheable=True)
    admission.token_arrived(token)
    group = next(iter(admission.groups.values()))
    group.state = GroupState.SERVER_DRAIN
    return admission


@pytest.mark.parametrize("load", [0, 2])
def test_live_server_startup_requires_operator_accounting(load: int) -> None:
    """Neither idle snapshots nor visible surviving work authorize a restart."""
    server = FakeServer(load=load)
    with pytest.raises(SccacheError, match="operator action"):
        _watch(server).drain_orphans()


def test_verified_stopped_server_allows_startup() -> None:
    """A manager-confirmed empty stop accounts for earlier cache requests."""
    server = FakeServer(stopped=True)
    _watch(server).drain_orphans()
    assert server.drain_queries == 1, "the stopped proof is checked before admission"


def test_direct_mode_never_queries_the_server() -> None:
    """A host that does not cache has no server work to drain."""
    server = FakeServer(load=SccacheError("unreachable manager"))
    _watch(server, mode="direct").drain_orphans()
    assert server.drain_queries == 0, "direct mode leaves the server alone"


def test_an_unknown_load_holds_the_server_drain_token() -> None:
    """A load that cannot be read returns no token."""
    watch = _watch(FakeServer(load=SccacheError("EACCES")), _draining(b"a"))
    assert not watch.drained_tokens(), "an unknown load is not a zero load"


def test_a_live_idle_server_retains_the_server_drain_token() -> None:
    """Accepted tasks waiting for jobserver tokens cannot be counted as complete."""
    watch = _watch(FakeServer(load=0), _draining(b"a"))
    assert not watch.drained_tokens(), "a live idle snapshot cannot return the token"
    assert watch.admission.tokens_held() == 1, "unknown detached work remains covered"


def test_verified_stopped_server_returns_the_token_once() -> None:
    """A stopped empty service accounts for all requests accepted before its stop."""
    watch = _watch(FakeServer(stopped=True), _draining(b"a"))
    assert watch.drained_tokens() == [ReturnToken(b"a")], (
        "verified stop returns ownership"
    )
    assert not watch.drained_tokens(), "the same token cannot be returned twice"
    assert watch.admission.tokens_held() == 0, (
        "no cached drain remains after accounting"
    )


def test_no_waiting_token_means_no_query() -> None:
    """The load is only read while a server-drain token waits."""
    watch = _watch(FakeServer(load=SccacheError("never read")))
    assert not watch.drained_tokens(), "nothing waits, so nothing is read"


@pytest.mark.parametrize(
    ("reachable", "mode", "cached"),
    [
        (True, "sccache", True),
        (False, "sccache", False),
        (SccacheError("cannot probe s.sock: EACCES"), "sccache", False),
        (True, "kache", True),
        (False, "kache", False),
        (SccacheError("cannot probe /run/kache/kache.sock"), "kache", False),
        (True, "direct", False),
    ],
    ids=[
        "reachable",
        "stopped",
        "probe-fault",
        "kache",
        "kache-unavailable",
        "kache-fault",
        "direct",
    ],
)
def test_new_groups_cache_only_through_a_reachable_server(
    reachable: bool | SccacheError, mode: str, cached: bool
) -> None:
    """A probe fault falls back to direct compilation like a stopped server.

    The probe is asked of whichever backend the mode selected, so an in-scope
    cache is composed on its own health rather than on a server this supervisor
    does not own. The managed server is scripted to the opposite answer, so a
    reading taken from the wrong port cannot produce the expected result.
    """
    admission = Admission(cached=not cached)
    _watch(
        FakeServer(reachable=reachable is not True),
        admission,
        mode,
        backend=FakeServer(reachable=reachable),
    ).refresh_cache_mode()
    assert admission.cached is cached, "the cache mode follows the probe"


def test_an_in_scope_cache_is_never_drained() -> None:
    """Only a detached backend has work outside a scope to account for.

    kache's compiler is a child of the wrapper inside the group, so there is
    nothing for start-up to account for and nothing for a stop to find. Asking
    the detached adapter here would be asking the wrong port.
    """
    server = FakeServer(load=SccacheError("nothing should read the server"))
    _watch(server, mode="kache").drain_orphans()
    assert server.drain_queries == 0, "an in-scope cache is never accounted for"


def test_a_probe_fault_is_reported_once(capsys: pytest.CaptureFixture[str]) -> None:
    """A persisting fault is logged when it appears, not on every poll."""
    watch = _watch(FakeServer(reachable=SccacheError("cannot probe s.sock: EACCES")))
    watch.refresh_cache_mode()
    watch.refresh_cache_mode()
    lines = capsys.readouterr().err.splitlines()
    assert lines == [
        "build-limits-supervisor: cannot probe s.sock: EACCES; compiling directly"
    ], "the fault is named once"


def test_metric_transitions_deduplicate_cache_polls(metrics, metric_sink) -> None:
    """Reachability and unknown load are counted only when their states change."""
    server = FakeServer(load=SccacheError("secret"), reachable=True)
    watch = _watch(server, _draining(b"a"))
    watch.metrics = metrics
    watch.refresh_cache_mode()
    watch.refresh_cache_mode()
    watch.drained_tokens()
    watch.drained_tokens()
    server.load = 0
    server.stopped = True
    watch.drained_tokens()
    watch.drained_tokens()
    assert [r["BUILD_LIMITS_EVENT"] for r in metric_sink.records] == [
        "cache_reachability",
        "cache_load_unknown",
        "cache_load_unknown",
        "server_drain_released",
    ], "polling doesn't create repeated transitions"
    assert metric_sink.records[-1]["BUILD_LIMITS_HELD"] == "0", (
        "release gauge is current"
    )
    assert metric_sink.records[-1]["BUILD_LIMITS_RELEASED"] == "1", "release count"


def test_repeated_query_faults_are_bounded_and_recovery_remains_conservative(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Retry exhaustion names an action, then recovered live work still retains tokens."""
    now = [0.0]
    server = FakeServer(load=SccacheError("injected transport refusal"))
    watch = _watch(server, _draining(b"a"))
    watch.clock = lambda: now[0]
    for _ in range(8):
        assert not watch.drained_tokens(), "failed accounting cannot return a token"
    assert server.drain_queries == 8, "the initial retries are finite"
    assert watch.recovery_action is not None, (
        "retry exhaustion requires explicit action"
    )
    assert watch.drain_attempts == 8, "the active fault remains visible in status"
    assert len(capsys.readouterr().err.splitlines()) == 2, (
        "one fault and one action suffice"
    )
    for _ in range(100):
        assert not watch.drained_tokens(), "backoff keeps unknown work covered"
    assert server.drain_queries == 8, "same-clock ticks cannot flood the manager"

    now[0] = 30.0
    server.load = 0
    assert not watch.drained_tokens(), "reachable live-idle cache still holds its token"
    assert watch.admission.tokens_held() == 1, "transport recovery is not completion"
    assert server.drain_queries == 9, "the next bounded retry observes recovery"

    now[0] = 60.0
    server.stopped = True
    assert watch.drained_tokens() == [ReturnToken(b"a")], (
        "verified empty stop returns once"
    )
    assert not watch.drained_tokens(), "accounted ownership stays released"
    assert watch.recovery_action is None, "successful accounting clears the action"
    assert watch.drain_fault is None, "successful accounting clears the current fault"


def test_live_cache_drain_diagnostic_does_not_report_a_manager_query_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A successful live-cache query identifies retained work without releasing tokens."""
    server = ManagedSccache(
        "fixture.service",
        Path("/fixture/socket"),
        UserManager(
            FakeRunner(stdout="MainPID=1\nControlGroup=\nActiveState=active\n")
        ),
    )
    watch = ServerWatch(server, _draining(b"a"), "sccache", server)
    assert not watch.drained_tokens(), "a live cache cannot prove completed work"
    assert watch.drain_fault is not None, "the retained-work reason stays visible"
    assert "retained detached work" in watch.drain_fault, (
        "live work has a distinct reason"
    )
    lines = capsys.readouterr().err
    assert "cannot read" not in lines, "a successful query is not a transport fault"
    assert "retained detached work" in lines, "the journal explains token retention"
    assert watch.admission.tokens_held() == 1, "the diagnostic cannot reclaim work"


# Manager snapshots of a live idle service and of a stopped one, both over
# the fixture control group.
LIVE = {"MainPID": "4242", "ControlGroup": "/fixture.service", "ActiveState": "active"}
STOPPED = {
    "MainPID": "0",
    "ControlGroup": "/fixture.service",
    "ActiveState": "inactive",
}


def _scripted(properties: dict[str, str]) -> ManagedSccache:
    """Build a managed cache with one fixed manager snapshot."""
    text = "".join(f"{key}={value}\n" for key, value in properties.items())
    return ManagedSccache(
        "fixture.service", Path("/fixture/socket"), UserManager(FakeRunner(stdout=text))
    )


def test_a_live_idle_cache_reports_the_live_work_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live service with zero visible roots is live-work, never transport."""
    fixture_cgroup(tmp_path, monkeypatch)
    server = _scripted(LIVE)
    watch = ServerWatch(server, _draining(b"a"), "sccache", server)
    assert not watch.drained_tokens(), "a live cache cannot prove completed work"
    assert watch.drain_fault_kind == "live_work", "live work is classified as such"
    assert watch.admission.tokens_held() == 1, "the kind never releases a token"


def test_a_stopped_but_populated_cache_reports_its_own_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopped service that still holds work needs cgroup inspection, not a stop."""
    fixture_cgroup(tmp_path, monkeypatch)
    server = _scripted(STOPPED)
    watch = ServerWatch(server, _draining(b"a"), "sccache", server)
    assert not watch.drained_tokens(), "surviving work keeps the token held"
    assert watch.drain_fault_kind == "stopped_populated", (
        "a stopped populated service is its own kind"
    )
    assert watch.admission.tokens_held() == 1, "the kind never releases a token"


def test_an_unavailable_manager_reports_the_manager_kind() -> None:
    """A transport refusal is a manager fault, never a live-work fault."""

    def runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Refuse every private and public query."""
        return subprocess.CompletedProcess(list(argv), 1, "", "Connection refused")

    server = ManagedSccache(
        "fixture.service", Path("/fixture/socket"), UserManager(runner)
    )
    watch = ServerWatch(server, _draining(b"a"), "sccache", server)
    assert not watch.drained_tokens(), "an unreadable state keeps the token held"
    assert watch.drain_fault_kind == "manager", "transport faults are manager faults"
    assert watch.drain_attempts == 1, "one failed attempt is recorded"
    assert watch.drain_fault is not None, "the fault text is recorded"
    assert watch.admission.tokens_held() == 1, "the kind never releases a token"


def test_kind_specific_guidance_names_the_right_operator_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live work and manager faults must not share one recovery instruction."""
    fixture_cgroup(tmp_path, monkeypatch)
    server = _scripted(LIVE)
    live = ServerWatch(server, _draining(b"a"), "sccache", server)
    now = [0.0]
    live.clock = lambda: now[0]
    # The first eight failures exhaust the limit and install the backoff.
    for _ in range(8):
        live.drained_tokens()
    assert live.drain_attempts == 8, "the guidance needs the exhausted limit"
    assert "build-limits-recover" in (live.recovery_action or ""), (
        "live-work guidance names the recovery command"
    )

    def refuse(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Refuse every private and public manager query."""
        return subprocess.CompletedProcess(list(argv), 1, "", "Connection refused")

    unreadable = ManagedSccache(
        "fixture.service", Path("/fixture/socket"), UserManager(refuse)
    )
    manager = ServerWatch(unreadable, _draining(b"a"), "sccache", unreadable)
    manager.clock = lambda: now[0]
    for _ in range(8):
        manager.drained_tokens()
    action = manager.recovery_action or ""
    assert "build-limits-recover" not in action, (
        "a manager fault is not cured by the cache-recovery command"
    )
    assert "do not stop" in action, "manager guidance forbids stopping the cache"
