"""Tests for the human-readable ``build-limits-status`` summary.

Each fixture is a fixed status document, so its request identifiers and
elapsed times are stable; directories are a ``<project>`` placeholder rather
than an absolute path. The semantic assertions pin the line each variant is
about; the snapshot pins the whole summary an operator reads.
"""

import typing as typ

import pytest

from buildlimits.status_render import render

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

_WAITING = {
    "request": 7,
    "label": "serde",
    "directory": "<project>",
    "waiting_seconds": 12.5,
}
_RUNNING = {
    "group": "g3",
    "request": 5,
    "state": "running",
    "cached": True,
    "label": "tokio",
    "directory": "<project>",
    "running_seconds": 40.0,
}

STATES: dict[str, tuple[dict[str, object], str]] = {
    "descriptors": (
        {
            "healthy": True,
            "descriptors": {
                "open": 1277,
                "soft_limit": 524288,
                "hard_limit": 524288,
                "sampling_errno": None,
            },
        },
        "open descriptors 1277, soft limit 524288, hard limit 524288",
    ),
    "unavailable-descriptors": (
        {
            "healthy": True,
            "descriptors": {
                "open": None,
                "soft_limit": 524288,
                "hard_limit": 524288,
                "sampling_errno": 24,
            },
        },
        "open descriptors unavailable (errno 24)",
    ),
    "unlimited-descriptors": (
        {
            "healthy": True,
            "descriptors": {
                "open": 12,
                "soft_limit": -1,
                "hard_limit": -1,
                "sampling_errno": None,
            },
        },
        "open descriptors 12, soft limit unlimited, hard limit unlimited",
    ),
    "cached": (
        {"capacity": 4, "held": 0, "healthy": True, "caching": True},
        "capacity 4, held 0, healthy, caching through sccache",
    ),
    "unreachable-cache": (
        {
            "capacity": 4,
            "held": 0,
            "healthy": True,
            "caching": False,
            "cache_mode": "sccache",
        },
        "sccache unreachable, compiling directly",
    ),
    "direct": (
        {
            "capacity": 4,
            "held": 0,
            "healthy": True,
            "caching": False,
            "cache_mode": "direct",
        },
        "capacity 4, held 0, healthy, compiling directly",
    ),
    "unhealthy": (
        {"capacity": 4, "held": 1, "healthy": False, "caching": True},
        "UNHEALTHY",
    ),
    "health-reason": (
        {
            "capacity": 4,
            "held": 1,
            "healthy": False,
            "caching": True,
            "health_reason": "internal error; draining before exit",
        },
        "unhealthy reason: internal error; draining before exit",
    ),
    "drain-fault": (
        {
            "capacity": 4,
            "held": 1,
            "healthy": False,
            "caching": True,
            "drain_fault": "manager query failed: connection refused",
            "groups": [{**_RUNNING, "state": "server-drain"}],
        },
        "drain check failed; tokens retained: manager query failed: connection refused",
    ),
    "drain-fault-kind": (
        {
            "capacity": 4,
            "held": 1,
            "healthy": False,
            "caching": True,
            "drain_fault_kind": "live_work",
            "drain_fault": "retained detached work: live sccache service",
        },
        "drain fault kind: live_work",
    ),
    "live-work-guidance": (
        {
            "capacity": 4,
            "held": 1,
            "healthy": False,
            "caching": True,
            "drain_fault_kind": "live_work",
            "drain_fault": "retained detached work: live sccache service",
            "recovery_action": "Run build-limits-recover to stop the managed cache.",
        },
        "drain fault kind: live_work",
    ),
    "recovery-action": (
        {
            "capacity": 4,
            "held": 1,
            "healthy": False,
            "caching": True,
            "recovery_action": "restore manager query transport; do not release tokens",
        },
        "required recovery action: restore manager query transport; do not release tokens",
    ),
    "missing-group-label": (
        {
            "capacity": 1,
            "held": 1,
            "healthy": False,
            "caching": True,
            "groups": [{**_RUNNING, "state": "server-drain", "label": None}],
        },
        "g3 server-drain (cached): request metadata unavailable; retaining compiler token",
    ),
    "missing-group-directory": (
        {
            "capacity": 1,
            "held": 1,
            "healthy": False,
            "caching": True,
            "groups": [{**_RUNNING, "state": "server-drain", "directory": None}],
        },
        "g3 server-drain (cached): request metadata unavailable; retaining compiler token",
    ),
    "missing-group-duration": (
        {
            "capacity": 1,
            "held": 1,
            "healthy": True,
            "caching": True,
            "groups": [{**_RUNNING, "running_seconds": None}],
        },
        "g3 running (cached): tokio in <project>, duration unavailable",
    ),
    "queued": (
        {
            "capacity": 1,
            "held": 1,
            "healthy": True,
            "caching": True,
            "queued": [_WAITING],
        },
        "waiting 12.5s: serde in <project>",
    ),
    "recovering": (
        {
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
        },
        "cache recovery stopping: next attempt in 60.0s, attempts 2",
    ),
    "recovered": (
        {
            "capacity": 1,
            "held": 0,
            "healthy": True,
            "caching": True,
            # A finished transaction renders no recovery line: the summary an
            # operator reads after a successful recovery is the healthy one.
            "cache_recovery": {
                "phase": "idle",
                "attempts": 0,
                "last_fault": None,
                "retry_in": 0.0,
            },
        },
        "capacity 1, held 0, healthy, caching through sccache",
    ),
    "group": (
        {
            "capacity": 1,
            "held": 1,
            "healthy": True,
            "caching": True,
            "groups": [_RUNNING],
        },
        "g3 running (cached): tokio in <project>, 40.0s",
    ),
}


@pytest.mark.parametrize("name", STATES)
def test_the_summary_describes_each_state(
    name: str, snapshot: SnapshotAssertion
) -> None:
    """Every health, cache, queue and group variant renders as expected."""
    state, expected_line = STATES[name]
    rendered = render(state)
    assert expected_line in rendered, rendered
    assert len(rendered.splitlines()) >= 3, "summary, queue and group sections"
    assert rendered == snapshot, "the rendered summary changed"


def test_the_fault_kind_renders_above_the_recovery_action() -> None:
    """The kind stays beside the action so guidance is read with its cause."""
    state, _line = STATES["live-work-guidance"]
    lines = render(state).splitlines()
    kind = next(
        i for i, line in enumerate(lines) if line.startswith("drain fault kind")
    )
    action = next(
        i for i, line in enumerate(lines) if line.startswith("required recovery action")
    )
    assert kind < action, "the fault kind must precede the action it explains"


def test_a_running_recovery_renders_its_fault_below_the_phase() -> None:
    """A transaction's fault stays beside the phase and retry it interrupted."""
    state, _line = STATES["recovering"]
    rendered = render(state).splitlines()
    phase = rendered.index("cache recovery stopping: next attempt in 60.0s, attempts 2")
    assert rendered[phase + 1] == (
        "cache recovery last fault: cannot stop the managed cache"
    ), "the fault is rendered immediately after the phase it stopped"


def test_a_live_work_fault_on_a_healthy_supervisor_reports_it_healthy() -> None:
    """A retained drain is not a health failure, so the summary says healthy.

    This is the reading the users' guide shows for a retained managed cache:
    the fault and its guidance render below a summary that stays healthy.
    """
    state, _line = STATES["live-work-guidance"]
    lines = render({**state, "healthy": True}).splitlines()
    assert lines[0] == "capacity 4, held 1, healthy, caching through sccache", (
        "the summary keeps the supervisor's own health"
    )
    assert "drain fault kind: live_work" in lines, "the fault is still reported"


def test_a_resting_trigger_reports_when_it_may_act_again() -> None:
    """An idle transaction still inside its cooldown says when it may act.

    Without the line, tokens retained beside an idle transaction read as a
    trigger that never fires, and the waiting builds cannot tell the two apart.
    """
    state, _line = STATES["live-work-guidance"]
    resting = {
        **state,
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
    """The fault a refused restart ended on outlives the transaction and its rest.

    The operator command reads the same document, but the status summary is
    what most people look at; a faulted ending must not read as a clean one.
    """
    ended = {
        "phase": "idle",
        "attempts": 0,
        "last_fault": "cannot start cache.service: Job failed",
        "retry_in": 0.0,
        "cooling_for": 900.0,
    }
    state, _line = STATES["live-work-guidance"]
    resting = render({**state, "cache_recovery": ended}).splitlines()
    assert "cache recovery idle: automatic recovery may start in 900.0s" in resting, (
        "the rest is shown"
    )
    assert (
        "cache recovery last fault: cannot start cache.service: Job failed" in resting
    ), "the fault is shown beside the rest"
    rested = render({**state, "cache_recovery": {**ended, "cooling_for": 0.0}})
    assert (
        "cache recovery last fault: cannot start cache.service: Job failed"
        in rested.splitlines()
    ), "the fault is still shown once the rest has passed"


def test_visible_roots_delaying_the_stop_are_reported() -> None:
    """An operator can see that live roots are what delays the automatic stop."""
    state, _line = STATES["live-work-guidance"]
    stopping = {
        "phase": "stopping",
        "attempts": 2,
        "last_fault": "the visible compiler roots proof lapsed before stopping",
        "retry_in": 30.0,
        "cooling_for": 0.0,
        "roots_waited": 120.0,
    }
    lines = render({**state, "cache_recovery": stopping}).splitlines()
    assert any("roots have delayed the stop for 120.0s" in line for line in lines), (
        "the time roots have delayed the stop is shown"
    )
    quiet = render({**state, "cache_recovery": {**stopping, "roots_waited": 0.0}})
    assert "roots have delayed" not in quiet, "no delay, no line"
