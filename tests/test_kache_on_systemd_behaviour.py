"""Acceptance scenarios for kache composition on a real systemd user manager.

ADR 005 makes kache an in-scope backend: the wrapper runs the compiler as its
own child inside the group's scope, and every group is pointed at a daemon this
supervisor never starts or stops. These scenarios are the end-to-end evidence
for that shape, including the two ways it degrades: a kache that cannot serve a
build, and a kache process no admitted scope accounts for.
"""

import socket
import typing as typ

import pytest
from harness_config import KacheSettings
from harness_processes import journal_records
from kache_stray_fixture import Stray, is_observable, remove, stray_kache_scope
from pytest_bdd import given, parsers, scenarios, then, when
from systemd_harness import Harness, systemd_user_marks, wait_for
from systemd_world import World as BaseWorld
from systemd_world import parent_script, scenario_world

from buildlimits import cgroups

# A cancelled group is escalated after the harness's two-second grace, and the
# stray guard only journals on its five-second poll.
pytestmark = [
    *systemd_user_marks("needs a systemd user manager and busctl"),
    pytest.mark.timeout(180),
]

scenarios("features/kache_on_systemd.feature")

#: What the adapter tells a build that kache could not serve.
NOTICE = b"kache is not serving compilations"

#: What the supervisor journals when the guard finds an uncovered process.
STRAY_LINE = "stray kache process outside admitted scopes"


class World(BaseWorld):
    """One scenario's supervisor, the builds it ran and anything it parked."""

    def __init__(self) -> None:
        """Create a world with nothing started."""
        super().__init__()
        self.stderr: bytes = b""
        self.stray: Stray | None = None

    def parked(self) -> Stray:
        """Read the stray a step parked.

        Returns
        -------
        Stray
            The parked process and its scope.

        Raises
        ------
        RuntimeError
            If a scenario asked for the stray before parking one.
        """
        if self.stray is None:
            msg = "a scenario step must park the stray first"
            raise RuntimeError(msg)
        return self.stray

    def close(self) -> None:
        """Remove the parked stray, then cancel and close everything else."""
        try:
            if self.stray is not None:
                remove(self.stray)
        finally:
            # The inherited close cancels builds and the supervisor. A stray
            # that will not go must not strand them for the rest of the
            # session, so cleanup order is not conditional on its success.
            super().close()


world = scenario_world(World)


def _daemon_answers(harness: Harness) -> bool:
    """Report whether the separately managed daemon still accepts connections."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        try:
            probe.connect(str(harness.kache_socket))
        except OSError:
            return False
    return True


def _metrics(harness: Harness, event: str) -> list[dict[str, typ.Any]]:
    """Return the supervisor unit's own records for one metric event.

    The supervisor's metric transport is trusted by the installed unit name,
    and a harness names its unit after its own tag, so the reported records
    are read from that unit's journal rather than through the reporter.
    """
    return [
        record
        for record in journal_records(harness.unit)
        if record.get("BUILD_LIMITS_SOURCE") == "supervisor"
        and record.get("BUILD_LIMITS_EVENT") == event
    ]


@given(
    parsers.re(r"a supervisor with (?P<slots>\d+) slots? composing builds with kache$")
)
def _kache_supervisor(world: World, slots: str) -> None:
    """Start a supervisor that composes builds with kache."""
    world.harness = Harness(int(slots), kache=KacheSettings(enabled=True))
    world.h.start()


@given(
    parsers.re(
        r"a supervisor with (?P<slots>\d+) slots? composing builds with kache "
        r"but no kache installed$"
    )
)
def _kache_missing(world: World, slots: str) -> None:
    """Start a supervisor configured for a kache client that cannot run."""
    world.harness = Harness(
        int(slots),
        kache=KacheSettings(
            enabled=True, executable="/nonexistent/kache", serving=False
        ),
    )
    world.h.start()


@given("a kache process parked outside the admitted scopes")
def _parked_stray(world: World) -> None:
    """Park a process named kache in a scope admission never held."""
    unit = f"buildlimits-stray-{world.h.tag}.scope"
    world.stray = stray_kache_scope(unit, world.h.compilers_slice, world.h.runtime)
    assert is_observable(world.stray.pid), (
        "the parked process is one the guard can match"
    )


@when("a build runs through kache")
def _kache_build(world: World) -> None:
    """Run a build that records its control group and its parent's command line."""
    world.run_ok(
        f"{parent_script(world)}; cat /proc/self/cgroup > {world.marker('cgroup')}"
    )
    parent = world.marker("parent").read_text()
    assert "fake_kache.py" in parent, parent


@then("the compiler ran inside the build's scope")
def _inside_scope(world: World) -> None:
    """Check the recorded control group is a supervisor scope in its slice."""
    path = cgroups.parse_unified_path(world.marker("cgroup").read_text())
    assert path is not None, "the compiler reported a unified control group"
    assert cgroups.is_scope_name(path.name), path
    assert path.parent.name == world.h.compilers_slice, path


@then("the kache daemon is still running")
def _daemon_alive(world: World) -> None:
    """Check the daemon the group was pointed at still answers."""
    assert wait_for(lambda: _daemon_answers(world.h)), (
        "the daemon outlives every group composed against it"
    )


@then("the slot is free again")
def _free(world: World) -> None:
    """Check that the token came back once the group's scope emptied."""
    world.assert_slot_free()


@when("a kache build starting a background process is cancelled")
def _cancel(world: World) -> None:
    """Run a build with a background child, then cancel it while it waits."""
    world.cancel_background_build()


@then("no process from that group survives")
def _gone(world: World) -> None:
    """Check the compiler and the background child both end with the group."""
    world.assert_background_group_gone()


@when("a build runs while kache is unavailable")
def _fallback_build(world: World) -> None:
    """Run a build while a configured kache cannot serve it."""
    _stdout, world.stderr = world.capture(parent_script(world))


@then("the build was not started through the kache client")
def _no_client(world: World) -> None:
    """Check the compiler's parent is not the kache wrapper."""
    world.assert_parent_lacks("kache")


@then("the build's standard error says kache is not serving compilations")
def _notice(world: World) -> None:
    """Check the adapter told the build that kache could not serve it."""
    assert NOTICE in world.stderr, world.stderr


@then("the build admits one direct fallback")
def _fallback_metric(world: World) -> None:
    """Check the supervisor metered the admission as an uncached fallback."""
    assert wait_for(lambda: bool(_metrics(world.h, "request_admitted")), timeout=20), (
        world.h.journal()
    )
    admissions = _metrics(world.h, "request_admitted")
    assert len(admissions) == 1, admissions
    assert admissions[0].get("BUILD_LIMITS_CACHE_PATH") == "direct_fallback", admissions
    assert admissions[0].get("BUILD_LIMITS_CACHE_BACKEND") == "kache", admissions


@then("status reports that stray process")
def _reported(world: World) -> None:
    """Check that status names the parked process and nothing else."""
    pid = world.parked().pid
    assert wait_for(lambda: world.h.status()["stray_kache_processes"] == [pid]), (
        world.h.status()
    )


@then("the supervisor's journal names that stray process")
def _journalled(world: World) -> None:
    """Check the guard's own poll reports the stray, as a fault and not a token."""
    pid = world.parked().pid
    line = f"{STRAY_LINE}: {pid}"
    assert wait_for(lambda: line in world.h.journal(), timeout=30), world.h.journal()


@when("a build runs while the stray is parked")
def _build_beside_stray(world: World) -> None:
    """Run an ordinary build while the unaccounted process is still there."""
    world.run_ok("true")


@then("the build still gets a slot")
def _slot(world: World) -> None:
    """Check the token returned while the stray was still unaccounted for."""
    pid = world.parked().pid
    world.assert_slot_free()
    status = world.h.status()
    assert status["stray_kache_processes"] == [pid], status
