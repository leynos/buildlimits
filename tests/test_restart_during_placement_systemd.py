"""Real-systemd acceptance: a restart that lands mid-placement loses no build.

The supervisor places each admitted request's held launch child in a transient
scope by running ``busctl``. Until that returns, the child and the ``busctl``
process are in the supervisor's own control group. On 2026-10-19, on host
``rohga``, ``systemctl restart`` landed in that window; the unit's default
``KillMode=control-group`` sent ``SIGTERM`` to the ``busctl`` as well, the
placement failed with an empty diagnostic, and the build was lost instead of
retried.

These scenarios hold the placement open with a ``busctl`` shim, restart the unit
while it waits, and then let it go. With the unit contract's ``KillMode=mixed``
only the supervisor is signalled and the placement completes. With the legacy
``control-group`` kill mode the placement is broken, and the supervisor must
turn that into the ``restarting`` refusal that the adapter retries.
"""

import typing as typ

import pytest
from busctl_shim import SlowBusctl
from restart_world import (
    assert_whole_again,
    finish,
    full_journal,
    started_marker,
    stopping,
)
from systemd_harness import systemd_user_marks, wait_for

if typ.TYPE_CHECKING:
    from pathlib import Path

    from restart_world import RestartWorld

pytestmark = [
    *systemd_user_marks("needs a systemd user manager and busctl"),
    pytest.mark.timeout(180),
]

SIGNAL_GRACE_SECONDS = 3.0


class Outcome(typ.NamedTuple):
    """What a restart that landed during placement did to the waiting build."""

    signalled: bool
    status: int
    stderr: str
    restart_status: int
    marker_lines: list[str]
    journal: str
    census: str


def restart_during_placement(
    world: RestartWorld, directory: Path, *, kill_mode: str | None
) -> Outcome:
    """Restart the supervisor while a build's scope placement is held open.

    Returns
    -------
    Outcome
        Whether the held ``busctl`` was signalled by the stop, how the build and
        the restart ended, how often the compiler ran, and the journal.
    """
    shim = SlowBusctl(directory / "shim")
    harness = world.supervisor(2, slow_busctl=shim, kill_mode=kill_mode)
    shim.raise_flag("hold")
    marker = started_marker(harness, "ran")
    build = world.build(harness, f"echo $$ >> {marker}; sleep 2; exit 7")
    assert wait_for(lambda: shim.seen("entered")), "placement never started"

    command = world.restart(harness)
    assert wait_for(lambda: stopping(harness), timeout=15), harness.journal()
    # A signal sent to the control group lands within moments of the stop job
    # starting. Waiting for it, rather than assuming it, makes the negative
    # case (nothing is signalled) as deliberate as the positive one.
    signalled = wait_for(lambda: shim.seen("terminated"), timeout=SIGNAL_GRACE_SECONDS)
    shim.raise_flag("release")

    status, err = finish(build)
    restart_status = command.wait(timeout=120)
    assert_whole_again(harness)
    lines = marker.read_text().split() if marker.exists() else []
    return Outcome(
        signalled,
        status,
        err,
        restart_status,
        lines,
        full_journal(harness),
        shim.census("terminated" if signalled else "entered"),
    )


def test_a_restart_during_placement_signals_only_the_supervisor(
    world: RestartWorld, tmp_path: Path
) -> None:
    """The unit contract keeps the stop away from the placement's helpers."""
    outcome = restart_during_placement(world, tmp_path, kill_mode=None)
    assert not outcome.signalled, "the stop signalled the busctl:\n" + outcome.census
    assert outcome.status == 7, outcome.stderr + outcome.journal
    assert outcome.restart_status == 0, "the restart command succeeded"
    assert len(outcome.marker_lines) == 1, "the compiler ran exactly once"
    assert "cannot create" not in outcome.journal, outcome.journal
    assert (
        "build-limits trace operation=scope_placement outcome=admitted "
        "failure_category=none stop_mode=graceful"
    ) in outcome.journal, "the completed placement span records the graceful stop"


def test_a_control_group_stop_during_placement_is_retried_not_lost(
    world: RestartWorld, tmp_path: Path
) -> None:
    """Defence in depth: a unit without the contract still loses nothing."""
    outcome = restart_during_placement(world, tmp_path, kill_mode="control-group")
    assert outcome.signalled, "the legacy kill mode signals the busctl"
    assert outcome.status == 7, outcome.stderr + outcome.journal
    assert outcome.restart_status == 0, "the restart command succeeded"
    assert len(outcome.marker_lines) == 1, "the compiler ran once, after the retry"
    assert "the supervisor is restarting" in outcome.stderr, outcome.stderr
    assert (
        "build-limits trace operation=scope_placement outcome=restarting "
        "failure_category=scope_placement stop_mode=graceful"
    ) in outcome.journal, "the broken legacy placement is classified for retry"
