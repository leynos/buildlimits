"""Behavioural scenarios for the status of a stale drain."""

import typing as typ

import pytest
from cache_recovery_harness import Harness  # ruff: ignore[typing-only-third-party-import] - pytest-bdd reads annotations
from cache_recovery_scenarios import busy_supervisor, idle_lines, status_lines
from pytest_bdd import given, parsers, scenarios, then, when

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


scenarios("features/stale_drain_status.feature")


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> cabc.Iterator[Harness]:
    """Provide the production supervisor over a busy host, past its first tick.

    Yields
    ------
    Harness
        The harness whose first poll has recorded the drain fault.
    """
    with busy_supervisor(tmp_path, monkeypatch) as busy:
        yield busy


@given("a busy host retaining one drained token while a build waits")
def _busy(harness: Harness) -> None:
    """Take the first poll, which starts the drain's age."""
    for _ in range(8):
        harness.tick()


@given("the recovery policy is manual")
def _manual(harness: Harness) -> None:
    """Switch the policy so no recovery happens by itself."""
    harness.supervisor.recovery.policy = "manual"


@when(parsers.parse("{seconds:d} seconds pass"))
def _pass(harness: Harness, seconds: int) -> None:
    """Let the drain age, polling once at the end."""
    harness.advance(float(seconds))
    harness.tick()


@then(parsers.parse("status says automatic recovery may start in {left}s"))
def _countdown(harness: Harness, left: str) -> None:
    """Check the idle countdown line."""
    expected = f"cache recovery idle: automatic recovery may start in {left}s"
    assert expected in status_lines(harness), status_lines(harness)


@then("status shows no recovery countdown")
def _no_countdown(harness: Harness) -> None:
    """Check that no line promises an automatic start."""
    assert not idle_lines(harness), "no line promises an automatic start"


@then(parsers.parse("the recovery action is labelled {label}"))
def _labelled(harness: Harness, label: str) -> None:
    """Check the label that says who must act."""
    lines = status_lines(harness)
    assert any(line.startswith(f"{label}: ") for line in lines), lines


@then("the recovery transaction has opened")
def _opened(harness: Harness) -> None:
    """Check that the stale proof opened the transaction."""
    assert harness.described()["phase"] == "quiescing", "the stale proof opened it"
