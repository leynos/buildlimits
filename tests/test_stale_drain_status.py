"""What ``build-limits-status`` says while a stale drain waits for recovery.

On host rohga a retained drain beside moving cached work, with requests
waiting, showed a mandatory ``build-limits-recover`` and no ``cache recovery``
line for up to five minutes while the automatic trigger was about to act. These
tests drive the incident through the production supervisor, then pin the
document, the plain-text lines and the journal. The 300 second limit is a
literal so a change to the production constant is a disagreement.
"""

import typing as typ

import pytest
from cache_recovery_scenarios import (
    busy_supervisor,
    idle_lines,
    status_lines,
    status_recovery,
)

from buildlimits import status
from buildlimits.status_render import render

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cache_recovery_harness import Harness
    from syrupy.assertion import SnapshotAssertion

COUNTDOWN = "cache recovery idle: automatic recovery may start in {}s"


def _exhaust_drain_retries(harness: Harness) -> None:
    """Fail the drain probe until the supervisor names a recovery action."""
    for _ in range(8):
        harness.tick()
    assert harness.supervisor.server.drain_attempts >= 8, "the probe gave up"


def test_a_busy_host_with_a_retained_drain_shows_the_countdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident: a visible root, moving cached work and a waiter."""
    with busy_supervisor(tmp_path, monkeypatch) as harness:
        harness.tick()
        harness.advance(100.0)
        harness.tick()
        recovery = status_recovery(harness)
        assert recovery["opens_in"] == pytest.approx(200.0), (
            "300 less the 100 seconds held"
        )
        assert recovery["pending"] is True, "automatic recovery is pending"
        assert COUNTDOWN.format(200.0) in status_lines(harness), (
            "status says the trigger is merely waiting"
        )


@pytest.mark.parametrize(("held", "left"), [(299.0, 1.0), (300.0, None)])
def test_the_countdown_ends_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: float, left: float | None
) -> None:
    """At 299 seconds one second remains; at 300 the transaction has opened."""
    with busy_supervisor(tmp_path, monkeypatch) as harness:
        harness.tick()
        harness.advance(held)
        harness.tick()
        recovery = harness.described()
        if left is None:
            assert recovery["phase"] == "quiescing", "the limit opens recovery"
            assert recovery["opens_in"] is None, "no countdown once it is open"
        else:
            assert recovery["phase"] == "idle", "one second short still waits"
            assert recovery["opens_in"] == left, "one second is left"


def test_the_recovery_action_says_recovery_is_automatic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator command is a fallback, not the required action."""
    with busy_supervisor(tmp_path, monkeypatch) as harness:
        _exhaust_drain_retries(harness)
        document = status.describe(harness.supervisor)
        action = str(document["recovery_action"])
        assert "pending" in action, action
        assert "fallback" in action, action
        assert document["recovery_action_is_fallback"] is True, "the label follows"
        lines = status_lines(harness)
        assert not any(line.startswith("required recovery action") for line in lines), (
            "a recovery that will happen on its own is not required of the operator"
        )
        assert any(line.startswith("fallback recovery action: ") for line in lines), (
            "the action line is labelled a fallback"
        )


def test_a_manual_policy_keeps_the_mandatory_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing recovers by itself under ``manual``, so the operator must act."""
    with busy_supervisor(tmp_path, monkeypatch) as harness:
        harness.supervisor.recovery.policy = "manual"
        _exhaust_drain_retries(harness)
        document = status.describe(harness.supervisor)
        recovery = status_recovery(harness)
        assert recovery["opens_in"] is None, "no countdown without the policy"
        assert recovery["automatic"] is False, "the document says why"
        assert "pending" not in str(document["recovery_action"]), "mandatory text"
        assert document["recovery_action_is_fallback"] is False, "not a fallback"
        assert any(
            line.startswith("required recovery action: Run build-limits-recover")
            for line in status_lines(harness)
        ), "the mandatory wording stays"
        assert not idle_lines(harness), "no countdown without the policy"


def test_exhausted_attempts_keep_the_mandatory_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transaction that gave up leaves the operator the only way out."""
    with busy_supervisor(tmp_path, monkeypatch) as harness:
        _exhaust_drain_retries(harness)
        harness.advance(300.0)
        harness.cache.refuse = "stop"
        harness.supervisor.recovery.request("manual", harness.now)
        harness.exhaust_retries()
        document = status.describe(harness.supervisor)
        recovery = status_recovery(harness)
        assert recovery["pending"] is False, "no attempts are left"
        assert document["recovery_action_is_fallback"] is False, "not a fallback"
        assert "pending" not in str(document["recovery_action"]), "mandatory text"


def test_a_resting_manual_policy_does_not_promise_an_automatic_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cooldown line is hidden when the trigger can never act."""
    with busy_supervisor(tmp_path, monkeypatch) as harness:
        harness.supervisor.recovery.policy = "manual"
        monkeypatch.setattr(harness.supervisor.recovery, "_cooling", harness.now + 42.5)
        assert not idle_lines(harness), "a manual policy never starts a recovery"


def test_the_cooldown_extends_the_countdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drain already stale waits only for the cooldown that remains."""
    with busy_supervisor(tmp_path, monkeypatch) as harness:
        harness.tick()
        harness.advance(299.0)
        monkeypatch.setattr(harness.supervisor.recovery, "_cooling", harness.now + 42.5)
        harness.tick()
        assert harness.described()["opens_in"] == pytest.approx(42.5), (
            "the cooldown is later"
        )
        assert COUNTDOWN.format(42.5) in status_lines(harness), "cooldown shown"


def test_the_journal_calls_a_pending_recovery_a_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The one journal line for the action says recovery is pending."""
    with busy_supervisor(tmp_path, monkeypatch) as harness:
        _exhaust_drain_retries(harness)
        journal = capsys.readouterr().err
    assert "drain requires operator action" not in journal, journal
    assert "operator action is only a fallback" in journal, journal


def test_the_pending_status_line_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot: SnapshotAssertion
) -> None:
    """Pin the lines an agent reads, with volatile values normalized."""
    with busy_supervisor(tmp_path, monkeypatch) as harness:
        _exhaust_drain_retries(harness)
        harness.advance(120.0)
        harness.tick()
        lines = [
            line
            for line in status_lines(harness)
            if line.startswith(("cache recovery", "fallback", "required", "drain"))
        ]
    assert lines == snapshot, "the pending-recovery lines changed"


def _document(**recovery: object) -> dict[str, object]:
    """Build a minimal status document around an idle cache recovery.

    Returns
    -------
    dict[str, object]
        The document, with the transaction fields overridden by ``recovery``.
    """
    return {
        "healthy": True,
        "cache_recovery": {
            "phase": "idle",
            "attempts": 0,
            "last_fault": None,
            "retry_in": 0.0,
            "cooling_for": 0.0,
            **recovery,
        },
    }


@pytest.mark.parametrize(
    ("recovery", "line"),
    [
        ({"opens_in": 200.0}, COUNTDOWN.format(200.0)),
        ({"opens_in": 200.0, "cooling_for": 30.0}, COUNTDOWN.format(200.0)),
        ({"opens_in": None, "cooling_for": 30.0}, None),
        ({"automatic": True, "cooling_for": 30.0}, COUNTDOWN.format(30.0)),
        ({"opens_in": None, "cooling_for": 0.0}, None),
        ({"opens_in": 0.0}, None),
        ({"automatic": False, "cooling_for": 30.0}, None),
    ],
)
def test_the_idle_line_shows_the_wait_only_when_there_is_one(
    recovery: dict[str, object], line: str | None
) -> None:
    """The countdown prefers the shared query and never outlives its trigger."""
    lines = render(_document(**recovery)).splitlines()
    shown = [item for item in lines if item.startswith("cache recovery idle")]
    assert shown == ([] if line is None else [line]), lines


def test_the_loop_action_is_never_called_a_fallback() -> None:
    """An internal-error action stays mandatory whatever recovery is doing."""
    document = {
        **_document(pending=True),
        "recovery_action": "Restore the failed supervisor control path.",
        "recovery_action_is_fallback": False,
    }
    assert "required recovery action: Restore" in render(document), (
        "only the cache-recovery text can be a fallback"
    )


def test_a_manager_fault_during_the_cooldown_shows_no_countdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rest that the trigger cannot end is not announced as a wait."""
    with busy_supervisor(tmp_path, monkeypatch) as harness:
        harness.tick()
        monkeypatch.setattr(harness.supervisor.recovery, "_cooling", harness.now + 60.0)
        assert idle_lines(harness), "a healthy retained drain counts down the cooldown"
        harness.supervisor.server.drain_fault_kind = "manager"
        assert status_recovery(harness)["cooling_for"] == pytest.approx(60.0), (
            "the cooldown itself is still reported"
        )
        assert not idle_lines(harness), "a faulted manager transport blocks the start"
