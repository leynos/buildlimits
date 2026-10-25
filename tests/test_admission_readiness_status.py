"""Exercise readiness against retained tokens and verified recovery."""

import collections.abc as cabc
import socket
import typing as typ
from pathlib import Path

import pytest
from cache_recovery_harness import hold_drained_group, queue_request

from buildlimits import status, status_cli
from buildlimits.admission import Group, GroupState, ReturnToken
from buildlimits.effects import Effect, Launch
from buildlimits.protocol import StatusReport, encode_reply
from buildlimits.status_client import query

if typ.TYPE_CHECKING:
    from cache_recovery_harness import Harness, HarnessFactory
    from syrupy.assertion import SnapshotAssertion

_STATUS_ENV = {"BUILD_LIMITS_SOCKET": "/run/test/supervisor.sock"}


def _query_through_status_frame(state: dict[str, object]) -> dict[str, object]:
    """Round-trip one status snapshot through the real framed status client."""
    client, server = socket.socketpair()
    server.sendall(encode_reply(StatusReport(state)))
    server.shutdown(socket.SHUT_WR)

    def connect(_path: Path, timeout: float) -> socket.socket:
        """Give the status client the scripted end of the socket pair."""
        client.settimeout(timeout)
        return client

    try:
        return query(env=_STATUS_ENV, connect=connect)
    finally:
        client.close()
        server.close()


def _status(harness: Harness) -> dict[str, object]:
    """Read the current supervisor snapshot without taking a token."""
    return status.describe(harness.supervisor)


def _fill_with_drains(harness: Harness, *, tokens: tuple[bytes, ...]) -> None:
    """Seed one accounted pool token and retained group per configured slot."""
    for index in range(harness.supervisor.settings.slots):
        token = tokens[index]
        harness.pool.request()
        group_id = f"g{index + 1}"
        hold_drained_group(harness.supervisor, group_id)
        harness.supervisor.admission.groups[group_id].token = token


def _returned_token_effects(effects: cabc.Sequence[Effect]) -> list[ReturnToken]:
    """Select only the pool-return effects from one effect batch."""
    return [effect for effect in effects if isinstance(effect, ReturnToken)]


def test_full_retention_blocks_readiness_until_verified_accounting(
    recovery_harnesses: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    snapshot: SnapshotAssertion,
) -> None:
    """A healthy, responsive supervisor can still require operator recovery."""
    harness = recovery_harnesses.build(mode="manual", slots=1)
    supervisor = harness.supervisor
    _fill_with_drains(harness, tokens=(b"+",))
    queue_request(supervisor)

    applied: list[Effect] = []
    apply = supervisor._apply

    def record_effects(effects: list[Effect]) -> None:
        """Observe effects while preserving the supervisor's real dispatch."""
        applied.extend(effects)
        apply(effects)

    monkeypatch.setattr(supervisor, "_apply", record_effects)
    harness.tick()

    blocked = _status(harness)
    admission = typ.cast("dict[str, object]", blocked["admission"])
    assert blocked["healthy"] is True, "the supervisor process remains healthy"
    assert blocked["held"] == blocked["capacity"] == 1, "every slot stays held"
    assert blocked["drain_fault_kind"] == "live_work", "the live cache owns the fault"
    assert "visible compiler roots=0" in str(blocked["drain_fault"]), (
        "the live service has no visible compiler roots"
    )
    assert admission == snapshot, "the blocked admission fields stay stable"

    assert (
        status_cli.main([], query=lambda: _query_through_status_frame(blocked)) == 0
    ), "health-only callers keep their historical exit"
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0].startswith("admission blocked:"), (
        "plain status leads with the admission block"
    )
    assert not captured.out.splitlines()[0].startswith("capacity"), (
        "the healthy headline cannot obscure the block"
    )
    assert (
        status_cli.main(["--ready"], query=lambda: _query_through_status_frame(blocked))
        == 3
    ), "readiness callers receive the operator-action exit"
    assert capsys.readouterr().out.splitlines()[0].startswith("admission blocked:"), (
        "the readiness invocation keeps the diagnostic visible"
    )

    for _ in range(3):
        harness.tick()
        still_blocked = _status(harness)
        assert (
            status_cli.main(
                ["--ready"],
                query=lambda state=still_blocked: _query_through_status_frame(state),
            )
            == 3
        ), "repeated successful queries remain not ready"
    assert not any(isinstance(effect, ReturnToken) for effect in applied), (
        "readiness reads never dispatch a token return"
    )
    assert not harness.pool.returned, "the live cache keeps its token"
    assert supervisor.admission.tokens_held() == 1, "held capacity stays unchanged"

    supervisor.recovery.request("manual", harness.now)
    harness.account()
    harness.tick()
    assert supervisor.server.drain_fault is None, "verified accounting clears the fault"
    assert _returned_token_effects(applied) == [ReturnToken(b"+")], (
        "verified accounting returns the token once"
    )
    assert harness.pool.returned == [b"+"], "the pool sees exactly one return"
    assert supervisor.admission.tokens_held() == 0, "the accounted group is removed"

    admission_effects = supervisor.admission.token_arrived(b"+")
    assert any(isinstance(effect, Launch) for effect in admission_effects), (
        "the waiting requester receives the returned token"
    )
    assert not supervisor.admission.queue, "the queued requester is admitted"
    ready = _status(harness)
    ready_admission = typ.cast("dict[str, object]", ready["admission"])
    assert ready_admission["state"] in {"ready", "saturated"}, (
        "normal admission resumes after accounting"
    )
    assert ready_admission["ready"] is True, "readiness returns without a restart"
    assert (
        status_cli.main(["--ready"], query=lambda: _query_through_status_frame(ready))
        == 0
    ), "the CLI reports readiness after verified recovery"


@pytest.mark.parametrize(
    "exhausted",
    [False, True],
    ids=["automatic-progress-pending", "automatic-attempts-exhausted"],
)
def test_starved_policy_keeps_a_progressing_drain_ready(
    recovery_harnesses: HarnessFactory,
    capsys: pytest.CaptureFixture[str],
    exhausted: bool,
) -> None:
    """An automatic recovery path stays ready while it makes progress."""
    harness = recovery_harnesses.build(mode="starved", slots=1)
    _fill_with_drains(harness, tokens=(b"+",))
    queue_request(harness.supervisor)
    if exhausted:
        harness.cache.refuse = "stop"

    harness.tick()
    if exhausted:
        harness.tick()
        harness.tick()
        harness.exhaust_retries()

    document = _status(harness)
    admission = typ.cast("dict[str, object]", document["admission"])
    recovery = typ.cast("dict[str, object]", document["cache_recovery"])
    assert admission["state"] == "draining", "automatic recovery has a progress path"
    assert admission["ready"] is True, "draining is usable readiness"
    assert recovery["automatic"] is True, (
        "the starved policy enables automatic recovery"
    )
    if exhausted:
        assert recovery["phase"] == "stopping", "the retrying phase remains active"
        attempts = recovery["attempts"]
        assert isinstance(attempts, int), "attempt count is an integer"
        assert attempts >= 8, "the automatic stop exhausted its attempts"
        assert recovery["pending"] is False, "the bounded attempt outlook is exhausted"
        assert not harness.pool.returned, "failed stops do not release retained tokens"
        assert harness.supervisor.admission.tokens_held() == 1, (
            "exhausted automatic retries keep every token held"
        )
    else:
        assert recovery["pending"] is True, "automatic recovery is due or running"
    assert (
        status_cli.main(
            ["--ready"], query=lambda: _query_through_status_frame(document)
        )
        == 0
    ), "a progressing automatic recovery exits ready"
    assert "admission draining:" in capsys.readouterr().out.splitlines()[0], (
        "plain rendering names the in-progress recovery"
    )


def test_declining_server_load_releases_oldest_drain_first(
    recovery_harnesses: HarnessFactory,
) -> None:
    """Falling server load moves full draining readiness to available capacity."""
    harness = recovery_harnesses.build(mode="starved", slots=2)
    supervisor = harness.supervisor
    _fill_with_drains(harness, tokens=(b"A", b"B"))
    queue_request(supervisor)
    harness.tick()

    draining = _status(harness)
    draining_admission = typ.cast("dict[str, object]", draining["admission"])
    assert draining_admission["state"] == "draining", "the automatic path is visible"
    assert (
        status_cli.main(
            ["--ready"], query=lambda: _query_through_status_frame(draining)
        )
        == 0
    ), "the automatic drain remains ready"

    first = supervisor.admission.server_activity(active_compiles=1)
    supervisor._apply(first)
    assert _returned_token_effects(first) == [ReturnToken(b"A")], (
        "the oldest token returns when one compile remains"
    )
    partially_drained = _status(harness)
    partial_admission = typ.cast("dict[str, object]", partially_drained["admission"])
    assert partial_admission["state"] == "ready", "partial retention leaves capacity"
    assert partial_admission["server_drain"] == 1, "one drain token remains accounted"

    last = supervisor.admission.server_activity(active_compiles=0)
    supervisor._apply(last)
    assert _returned_token_effects(last) == [ReturnToken(b"B")], (
        "the remaining token returns after the load reaches zero"
    )
    assert harness.pool.returned == [b"A", b"B"], "returns preserve oldest-first order"
    ready = _status(harness)
    ready_admission = typ.cast("dict[str, object]", ready["admission"])
    assert ready_admission["state"] == "ready", "no retained drain remains"
    assert ready_admission["ready"] is True, "the normal readiness state is restored"


def test_unreachable_cache_with_running_direct_fallback_is_ready(
    recovery_harnesses: HarnessFactory,
) -> None:
    """An unreachable cache does not block a running direct-fallback group."""
    harness = recovery_harnesses.build(mode="manual", slots=2)
    supervisor = harness.supervisor
    harness.pool.request()
    supervisor.admission.groups["g1"] = Group(
        "g1", 1, b"+", cached=False, state=GroupState.RUNNING
    )
    harness.supervisor.server.refresh_cache_mode()

    document = _status(harness)
    admission = typ.cast("dict[str, object]", document["admission"])
    assert document["caching"] is False, "the cache cannot be reached"
    assert admission["running"] == 1, "the direct fallback group is running"
    assert admission["state"] == "ready", "cache fallback leaves admission usable"
    assert admission["ready"] is True, "readiness ignores cache reachability"
