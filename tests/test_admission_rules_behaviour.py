"""Behavioural scenarios for the admission core."""

import pytest
from pytest_bdd import given, scenarios, then, when

from buildlimits.admission import (
    Admission,
    Effect,
    Launch,
    NotifyFinished,
    Refuse,
    ReturnToken,
    Shutdown,
    Terminate,
)
from buildlimits.effects import Exit

scenarios("features/admission_rules.feature")


class World:
    """The admission core and every effect it has produced."""

    def __init__(self) -> None:
        """Create a caching admission core with no effects yet."""
        self.admission = Admission(cached=True)
        self.effects: list[Effect] = []
        self.next_token = 0

    def of[E](self, kind: type[E]) -> list[E]:
        """Return the effects of one kind."""
        return [effect for effect in self.effects if isinstance(effect, kind)]


@pytest.fixture
def world() -> World:
    """Provide a fresh admission core."""
    return World()


@given("an admission core with caching enabled")
def _core(world: World) -> None:
    """Check that the admission core starts with caching enabled."""
    assert world.admission.cached, "the world starts with caching enabled"


@when("a build is submitted")
def _one(world: World) -> None:
    """Submit a single build."""
    world.effects.extend(world.admission.submit(1))


@when("two builds are submitted")
def _two(world: World) -> None:
    """Submit two builds."""
    world.effects.extend(world.admission.submit(1))
    world.effects.extend(world.admission.submit(2))


@when("the pool supplies one token")
def _token(world: World) -> None:
    """Supply one token from the pool."""
    world.next_token += 1
    world.effects.extend(world.admission.token_arrived(bytes([64 + world.next_token])))


@when("the second build is cancelled")
def _cancel_second(world: World) -> None:
    """Cancel the second build."""
    world.effects.extend(world.admission.cancel(2))


@when("the build is cancelled")
def _cancel_first(world: World) -> None:
    """Cancel the build."""
    world.effects.extend(world.admission.cancel(1))


@when("the group's leader exits and its scope empties")
def _drain(world: World) -> None:
    """End the group's leader by signal 15 and empty its scope."""
    world.effects.extend(world.admission.leader_exited("g1", Exit(signal=15)))
    world.effects.extend(world.admission.group_emptied("g1"))


@when("the group's leader is killed and its scope empties")
def _killed(world: World) -> None:
    """Kill the group's leader and empty its scope."""
    world.effects.extend(world.admission.leader_exited("g1", Exit(signal=9)))
    world.effects.extend(world.admission.group_emptied("g1"))


@when("the sccache server reports one live compilation")
def _busy(world: World) -> None:
    """Report one live compilation from the sccache server."""
    world.effects.extend(world.admission.server_activity(1))


@when("the sccache server reports no live compilations")
def _idle(world: World) -> None:
    """Report no live compilations from the sccache server."""
    world.effects.extend(world.admission.server_activity(0))


@when("the pool fails")
def _fail(world: World) -> None:
    """Fail the pool."""
    world.effects.extend(world.admission.health_failed())


@then("one compiler group is running")
def _running(world: World) -> None:
    """Check that one compiler group is running."""
    assert len(world.admission.groups) == 1, "one compiler group is running"


@then("one build is waiting without a token")
def _waiting(world: World) -> None:
    """Check that one build is waiting without a token."""
    assert list(world.admission.queue) == [2], "the second build waits without a token"


@then("only the first build is launched")
def _only_first(world: World) -> None:
    """Check that only the first build is launched."""
    assert [effect.request_id for effect in world.of(Launch)] == [1], (
        "only the first build is launched"
    )


@then("its group is terminated")
@then("the running group is terminated")
def _terminated(world: World) -> None:
    """Check that the group is terminated."""
    assert Terminate("g1") in world.effects, "the group is terminated"


@then("the token is still held")
def _held(world: World) -> None:
    """Check that the token is still held."""
    assert world.admission.tokens_held() == 1, "the token is still held"
    assert not world.of(ReturnToken), "no token has been returned yet"


@then("the token is returned")
def _returned(world: World) -> None:
    """Check that the token is returned."""
    assert world.of(ReturnToken) == [ReturnToken(b"A")], "the token is returned"
    assert world.admission.tokens_held() == 0, "no token remains held"


@then("the build's result is reported")
def _reported(world: World) -> None:
    """Check that the build's result is reported."""
    assert NotifyFinished(1, Exit(signal=9)) in world.effects, (
        "the build's result is reported"
    )


@then("the waiting build is refused")
def _refused(world: World) -> None:
    """Check that the waiting build is refused."""
    assert Refuse(2, "unhealthy") in world.effects, "the waiting build is refused"


@then("the supervisor shuts down")
def _shutdown(world: World) -> None:
    """Check that the supervisor shuts down."""
    assert Shutdown() in world.effects, "the supervisor shuts down"
