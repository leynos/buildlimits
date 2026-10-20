"""Group drain observations cannot release tokens or alter OS-call authority."""

import errno
import typing as typ
from pathlib import Path

import pytest

from buildlimits import cgroups
from buildlimits.admission import Group, GroupState
from buildlimits.config import SupervisorConfig
from buildlimits.groups import Running
from buildlimits.supervisor import Supervisor, SupervisorTelemetry

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from conftest import RecordingSink

    from buildlimits.groups import Groups
    from buildlimits.launch import HeldChild
    from buildlimits.metrics import Metrics


@pytest.fixture
def now() -> list[float]:
    """Supply a mutable monotonic sample shared by group deadlines."""
    return [0.0]


@pytest.fixture
def supervisor(metrics: Metrics, now: list[float]) -> cabc.Iterator[Supervisor]:
    """Provide one real admitted draining group over inert child ownership."""
    instance = Supervisor(
        SupervisorConfig(cache_mode="direct"),
        clock=lambda: now[0],
        telemetry=SupervisorTelemetry(metrics=metrics),
    )
    instance.admission.groups["g1"] = Group(
        "g1", 1, b"a", False, state=GroupState.DRAINING
    )
    instance.groups.running["g1"] = Running(
        "g1", 1, Path("/secret/scope"), typ.cast("HeldChild", object())
    )
    yield instance
    instance.selector.close()


def test_group_calls_preserve_pending_work_and_escalation_deadlines(
    monkeypatch: pytest.MonkeyPatch,
    supervisor: Supervisor,
    now: list[float],
    metric_sink: RecordingSink,
) -> None:
    """Termination success never proves empty scope or returns a token."""
    controls: list[tuple[Path, bool]] = []
    populated = [True]

    def terminate(path: Path, *, force: bool) -> None:
        """Record the actual bounded control operation once."""
        controls.append((path, force))

    monkeypatch.setattr(cgroups, "terminate", terminate)
    monkeypatch.setattr(cgroups, "is_populated", lambda _path: populated[0])
    supervisor.groups.terminate("g1")
    running = supervisor.groups.running["g1"]
    assert running.kill_at == pytest.approx(supervisor.settings.cancel_grace_seconds), (
        "termination retains the original escalation deadline"
    )
    now[0] = supervisor.settings.cancel_grace_seconds
    assert supervisor.groups.advance({"g1"}) == [], "populated scopes remain held"
    assert [force for _path, force in controls] == [False, True], (
        "one termination and one due escalation reach the OS boundary"
    )
    assert running.kill_at is None, "successful escalation clears its old deadline"
    populated[0] = False
    assert supervisor.groups.advance({"g1"}) == ["g1"], "verified empty scopes report"
    assert supervisor.admission.tokens_held() == 1, (
        "only admission can return the token"
    )
    records = metric_sink.records
    assert [record["BUILD_LIMITS_DRAIN_OPERATION"] for record in records] == [
        "terminate",
        "escalate",
        "empty_check",
        "empty_check",
    ], "observations follow actual operation order"
    assert records[2]["BUILD_LIMITS_DRAIN_STATE"] == "pending", (
        "a populated scope is ordinary live work"
    )
    assert records[2]["BUILD_LIMITS_DRAIN_FAILURES"] == "0", (
        "pending live work is not a failed emptiness probe"
    )
    assert "secret" not in str(records), "scope paths never enter observations"


@pytest.mark.parametrize(
    "case",
    [
        ("terminate", False, lambda groups: groups.terminate("g1")),
        ("escalate", True, lambda groups: groups.advance(set())),
    ],
)
def test_group_faults_preserve_errors_and_recover_after_real_success(
    monkeypatch: pytest.MonkeyPatch,
    supervisor: Supervisor,
    metric_sink: RecordingSink,
    case: tuple[str, bool, cabc.Callable[[Groups], object]],
) -> None:
    """A failed control call retains its scope and recovers only after OS success."""
    operation, expected_force, action = case
    failure = OSError(errno.EIO, "secret control failure")
    calls: list[tuple[Path, bool]] = []

    def terminate(path: Path, *, force: bool) -> None:
        """Fail the first actual control call and succeed on its retry."""
        calls.append((path, force))
        if len(calls) == 1:
            raise failure

    monkeypatch.setattr(cgroups, "terminate", terminate)
    running = supervisor.groups.running["g1"]
    running.kill_at = 0.0
    with pytest.raises(OSError, match="secret control failure") as caught:
        action(supervisor.groups)
    assert caught.value is failure, "observation must preserve the original exception"
    assert supervisor.admission.tokens_held() == 1, "faults cannot reclaim a token"
    action(supervisor.groups)
    selected = [
        record
        for record in metric_sink.records
        if record["BUILD_LIMITS_DRAIN_OPERATION"] == operation
    ]
    assert [record["BUILD_LIMITS_DRAIN_STATE"] for record in selected] == [
        "failed",
        "recovered",
    ], "only successful control calls clear their own observation faults"
    assert selected[0]["BUILD_LIMITS_DRAIN_REASON"] == "os_error", (
        "the reason category excludes arbitrary exception content"
    )
    assert calls == [(running.scope_dir, expected_force)] * 2, (
        "both actual calls retain the exact scope, force and single-action count"
    )
    assert "secret" not in str(selected), "raw control errors never enter fields"


def test_empty_check_recovers_only_after_verified_empty_scope(
    monkeypatch: pytest.MonkeyPatch,
    supervisor: Supervisor,
    metric_sink: RecordingSink,
) -> None:
    """A successful but populated check cannot clear a previous probe fault."""
    cause = RuntimeError("secret original cause")
    failure = OSError(errno.EIO, "secret probe fault")
    failure.__cause__ = cause
    outcomes: list[bool | OSError] = [failure, True, False]
    calls: list[Path] = []

    def populated(path: Path) -> bool:
        """Raise once, report live work, then verify emptiness."""
        calls.append(path)
        outcome = outcomes.pop(0)
        if isinstance(outcome, OSError):
            raise outcome
        return outcome

    monkeypatch.setattr(cgroups, "is_populated", populated)
    with pytest.raises(OSError, match="secret probe fault") as caught:
        supervisor.groups.advance({"g1"})
    assert caught.value is failure and caught.value.__cause__ is cause, (
        "probe observations preserve the original exception and chained cause"
    )
    assert supervisor.groups.advance({"g1"}) == [], "live work remains pending"
    assert supervisor.groups.advance({"g1"}) == ["g1"], "empty verification completes"
    assert [record["BUILD_LIMITS_DRAIN_STATE"] for record in metric_sink.records] == [
        "failed",
        "pending",
        "recovered",
    ], "actual empty verification is the sole recovery boundary"
    assert supervisor.admission.tokens_held() == 1, (
        "only admission can release the token"
    )

    assert calls == [supervisor.groups.running["g1"].scope_dir] * 3, (
        "each actual probe runs once on the original scope"
    )
    assert metric_sink.records[0]["BUILD_LIMITS_DRAIN_REASON"] == "os_error", (
        "failed probes retain their fixed OS-error category"
    )
    assert "secret" not in str(metric_sink.records), (
        "probe error messages and causes never enter bounded fields"
    )
