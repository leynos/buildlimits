"""Tests for recovery-specific status diagnostics."""

from buildlimits.status_render import render

_LIVE_WORK_STATUS = {
    "capacity": 4,
    "held": 1,
    "healthy": False,
    "caching": True,
    "drain_fault_kind": "live_work",
    "drain_fault": "retained detached work: live sccache service",
    "recovery_action": "Run build-limits-recover to stop the managed cache.",
}

_RECOVERING_STATUS = {
    "capacity": 1,
    "held": 1,
    "healthy": True,
    "caching": True,
    "cache_recovery": {
        "phase": "stopping",
        "attempts": 2,
        "last_fault": "cannot stop the managed cache",
        "retry_in": 60.0,
    },
}


def test_the_fault_kind_renders_above_the_recovery_action() -> None:
    """The kind stays beside the action so guidance is read with its cause."""
    lines = render(_LIVE_WORK_STATUS).splitlines()
    kind = next(
        i for i, line in enumerate(lines) if line.startswith("drain fault kind")
    )
    action = next(
        i for i, line in enumerate(lines) if line.startswith("required recovery action")
    )
    assert kind < action, "the fault kind must precede the action it explains"


def test_a_running_recovery_renders_its_fault_below_the_phase() -> None:
    """A transaction's fault stays beside the phase and retry it interrupted."""
    rendered = render(_RECOVERING_STATUS).splitlines()
    phase = rendered.index("cache recovery stopping: next attempt in 60.0s, attempts 2")
    assert rendered[phase + 1] == (
        "cache recovery last fault: cannot stop the managed cache"
    ), "the fault is rendered immediately after the phase it stopped"


def test_a_resting_trigger_reports_when_it_may_act_again() -> None:
    """An idle transaction still inside its cooldown says when it may act.

    Without the line, tokens retained beside an idle transaction read as a
    trigger that never fires, and the waiting builds cannot tell the two apart.
    """
    resting = {
        **_LIVE_WORK_STATUS,
        "cache_recovery": {
            "phase": "idle",
            "attempts": 0,
            "last_fault": None,
            "retry_in": 0.0,
            "cooling_for": 42.5,
        },
    }
    lines = render(resting).splitlines()
    assert "cache recovery idle: automatic recovery may start in 42.5s" in lines, (
        "the remaining cooldown is shown"
    )
    rested = {
        **resting,
        "cache_recovery": {**resting["cache_recovery"], "cooling_for": 0.0},
    }
    assert not any(
        line.startswith("cache recovery") for line in render(rested).splitlines()
    ), "a trigger free to act adds no recovery line"


def test_a_refused_restart_stays_visible_after_the_transaction_ends() -> None:
    """A refused restart fault outlives the transaction and its rest."""
    ended = {
        "phase": "idle",
        "attempts": 0,
        "last_fault": "cannot start cache.service: Job failed",
        "retry_in": 0.0,
        "cooling_for": 900.0,
    }
    resting = render({**_LIVE_WORK_STATUS, "cache_recovery": ended}).splitlines()
    assert "cache recovery idle: automatic recovery may start in 900.0s" in resting, (
        "the rest is shown"
    )
    assert (
        "cache recovery last fault: cannot start cache.service: Job failed" in resting
    ), "the fault is shown beside the rest"
    rested = render({
        **_LIVE_WORK_STATUS,
        "cache_recovery": {**ended, "cooling_for": 0.0},
    })
    assert (
        "cache recovery last fault: cannot start cache.service: Job failed"
        in rested.splitlines()
    ), "the fault is still shown once the rest has passed"


def test_visible_roots_delaying_the_stop_are_reported() -> None:
    """An operator can see that live roots delay the automatic stop."""
    stopping = {
        "phase": "stopping",
        "attempts": 2,
        "last_fault": "the visible compiler roots proof lapsed before stopping",
        "retry_in": 30.0,
        "cooling_for": 0.0,
        "roots_waited": 120.0,
    }
    lines = render({**_LIVE_WORK_STATUS, "cache_recovery": stopping}).splitlines()
    assert any("roots have delayed the stop for 120.0s" in line for line in lines), (
        "the time roots have delayed the stop is shown"
    )
    quiet = render({
        **_LIVE_WORK_STATUS,
        "cache_recovery": {**stopping, "roots_waited": 0.0},
    })
    assert "roots have delayed" not in quiet, "no delay, no line"
