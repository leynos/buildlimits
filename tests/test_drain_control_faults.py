"""Control diagnostics cannot lose already reaped exits or admission health."""

import errno
import os
import typing as typ
from pathlib import Path

import pytest
from pool_helpers import attach_pool

from buildlimits import supervisor as supervisor_module
from buildlimits.admission import Group, GroupState, ReturnToken
from buildlimits.config import SupervisorConfig
from buildlimits.effects import Exit
from buildlimits.groups import Groups, Running
from buildlimits.supervisor import Supervisor

if typ.TYPE_CHECKING:
    from buildlimits.launch import HeldChild


def _silence_signal_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the self-pipe's descriptor when no pipe was registered."""
    monkeypatch.setattr(os, "read", lambda *_args: b"")


def test_unhealthy_journal_fault_preserves_token_and_health_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed diagnostic cannot leave unaccounted cached work open to admission."""
    instance = Supervisor(SupervisorConfig())
    with instance.selector:
        instance.admission.groups["g1"] = Group(
            "g1", 1, b"+", True, state=GroupState.SERVER_DRAIN
        )

        def fail(_message: str) -> None:
            """Refuse only a nonessential journal write."""
            raise BrokenPipeError(errno.EPIPE, "injected journal failure")

        monkeypatch.setattr(supervisor_module, "log", fail)
        instance._unhealthy("injected control fault")
        assert not instance.admission.healthy, "admission stops before diagnostics"
        assert instance.admission.tokens_held() == 1, (
            "unknown cached work retains its token"
        )
        assert instance.exit_status is None, (
            "a diagnostic failure cannot authorize exit"
        )


def test_diagnostic_fault_cannot_lose_an_already_reaped_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumed wait status remains accounted for before diagnostic handling."""
    instance = Supervisor(SupervisorConfig(cache_mode="direct"))
    with instance.selector:
        instance.admission.groups["g1"] = Group("g1", 1, b"+", False)
        running = Running(
            "g1", 1, Path("/fixture/scope"), typ.cast("HeldChild", object())
        )
        terminated: list[str] = []
        _silence_signal_pipe(monkeypatch)
        # ``Groups`` is slotted, so a method is substituted on the class; the
        # patch receives the instance the real method would have.
        monkeypatch.setattr(Groups, "reap", lambda _groups: [(running, Exit(code=0))])
        monkeypatch.setattr(
            Groups, "terminate", lambda _groups, group_id: terminated.append(group_id)
        )

        def fail(_group_id: str) -> None:
            """Fail after the operating-system exit has already been consumed."""
            raise OSError(errno.EIO, "injected error-channel fault")

        monkeypatch.setattr(instance, "_exec_outcome", fail)
        with pytest.raises(OSError, match="injected error-channel"):
            instance._reap_leaders()
        group = instance.admission.groups["g1"]
        assert group.state is GroupState.DRAINING, (
            "known exit survives diagnostic failure"
        )
        assert group.exit == Exit(code=0), "the consumed exit is never invented or lost"
        assert terminated == ["g1"], "scope cleanup remains scheduled despite the fault"
        assert instance.admission.tokens_held() == 1, (
            "token return still waits for empty scope"
        )


def test_tick_reconciles_only_lost_cancellation_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing termination effects are retried while ordinary running work continues."""
    instance = Supervisor(SupervisorConfig(cache_mode="direct"))
    with instance.selector:
        instance.admission.groups["cancelled"] = Group(
            "cancelled", 1, b"a", False, cancelled=True
        )
        instance.admission.groups["ordinary"] = Group("ordinary", 2, b"b", False)
        instance.admission.groups["cached"] = Group(
            "cached", 3, b"c", True, state=GroupState.SERVER_DRAIN, cancelled=True
        )
        instance._next_server_poll = float("inf")
        for name, group in instance.admission.groups.items():
            instance.groups.running[name] = Running(
                name,
                group.request_id,
                Path("/fixture") / name,
                typ.cast("HeldChild", object()),
            )
        terminated: list[str] = []
        monkeypatch.setattr(
            Groups, "terminate", lambda _groups, group_id: terminated.append(group_id)
        )
        monkeypatch.setattr(Groups, "advance", lambda _groups, _draining: [])
        instance._tick()
        assert terminated == ["cancelled"], (
            "reconciliation cannot interrupt normal work"
        )
        assert instance.admission.tokens_held() == 3, (
            "termination is not completion proof"
        )


def test_tick_completes_unhealthy_shutdown_only_after_complete_accounting() -> None:
    """Lost final effects recover without treating cached drain groups as empty."""
    instance = Supervisor(SupervisorConfig(cache_mode="direct"))
    with instance.selector:
        instance.admission.healthy = False
        instance.admission.groups["g1"] = Group(
            "g1", 1, b"+", True, state=GroupState.SERVER_DRAIN
        )
        instance._next_server_poll = float("inf")
        instance._tick()
        assert instance.exit_status is None, "server-drain ownership prevents exit"
        pool = attach_pool(instance)
        effects = instance.admission.server_activity(0)
        for effect in effects:
            if isinstance(effect, ReturnToken):
                instance._return_token(effect.token)
        # Simulate a fault between the completed token return and Shutdown.
        assert pool.returned == [b"+"], "the known completed token already returned"
        instance._tick()
        assert instance.exit_status == 1, "accounted unhealthy completion stays failing"


def test_one_diagnostic_fault_cannot_discard_other_collected_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All statuses collected in one reap remain accounted despite an early fault."""
    instance = Supervisor(SupervisorConfig(cache_mode="direct"))
    with instance.selector:
        ended = []
        for number in (1, 2):
            name = f"g{number}"
            instance.admission.groups[name] = Group(
                name, number, bytes([number]), False
            )
            running = Running(
                name, number, Path("/fixture") / name, typ.cast("HeldChild", object())
            )
            ended.append((running, Exit(code=number)))
        terminated: list[str] = []
        diagnosed: list[str] = []
        _silence_signal_pipe(monkeypatch)
        monkeypatch.setattr(Groups, "reap", lambda _groups: ended)
        monkeypatch.setattr(
            Groups, "terminate", lambda _groups, group_id: terminated.append(group_id)
        )

        def diagnose(name: str) -> None:
            """Fault only the first group's diagnostic after both exits were collected."""
            diagnosed.append(name)
            if name == "g1":
                raise OSError(errno.EIO, "injected first diagnostic fault")

        monkeypatch.setattr(instance, "_exec_outcome", diagnose)
        with pytest.raises(OSError, match="injected first diagnostic"):
            instance._reap_leaders()
        assert diagnosed == ["g1", "g2"], (
            "the first fault cannot discard later diagnostics"
        )
        assert terminated == ["g1", "g2"], "every observed scope receives cleanup"
        for number in (1, 2):
            group = instance.admission.groups[f"g{number}"]
            assert group.state is GroupState.DRAINING, "every consumed exit is retained"
            assert group.exit == Exit(code=number), (
                "the correct exit remains associated"
            )
        assert instance.admission.tokens_held() == 2, "both tokens await empty scopes"


def test_fault_during_orderly_drain_keeps_unhealthy_reason() -> None:
    """A subsequent control failure remains visible while an orderly drain waits."""
    instance = Supervisor(SupervisorConfig())
    with instance.selector:
        instance.admission.groups["g1"] = Group(
            "g1", 1, b"+", True, state=GroupState.SERVER_DRAIN
        )
        instance.stop()
        instance._unhealthy("drain control unavailable")
        assert instance.health_reason == "drain control unavailable", (
            "already-disabled admission still records its new infrastructure fault"
        )
        assert instance.admission.tokens_held() == 1, (
            "unknown work remains accounted for"
        )
        assert instance.exit_status is None, "failure cannot end an unaccounted drain"
