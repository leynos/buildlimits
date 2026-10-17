"""Composition permission at the moment a queued token becomes a launch.

The admission core already decides composition from two things: whether the
request may be cached and whether the server was last found reachable. This
module covers the third answer, which is a permission rather than a reading:
a recovery holds composition out of service for an interval, and a token that
spent that interval waiting must not be composed from the stale reading it was
granted under.

The permission is consulted where the launch is composed rather than where the
token is requested, which is what these tests pin: a token already in flight,
or one that arrives after the recovery started, is judged at the last instant
the answer is still current. Every test states the *pair* of decisions — the
composition flag and the group's own record of it — because a launch that ran
directly while its group claimed to be cached would retain the token for
server accounting that nothing needs.
"""

from buildlimits.admission import Admission, GroupState
from buildlimits.effects import Exit, Launch, ReturnToken


def _composed(permission: list[bool]) -> tuple[Admission, list[object]]:
    """Admit one cacheable request under a permission the test can flip.

    Parameters
    ----------
    permission : list[bool]
        A one-element list holding the answer the composition permission
        reports, so a test can withdraw or grant it between events.

    Returns
    -------
    tuple[Admission, list[object]]
        The admission state and the effects of the launch it produced.
    """
    admission = Admission(cached=True, cache_allowed=lambda: permission[0])
    admission.submit(1, cacheable=True)
    return admission, list(admission.token_arrived(b"+"))


def _launched(admission: Admission) -> Launch:
    """Grant one token and return the launch the waiting request receives.

    Returns
    -------
    Launch
        The launch effect, so a test can read the composition decision.

    Raises
    ------
    TypeError
        If the token produced anything but a launch.
    """
    launch = admission.token_arrived(b"+")[0]
    if not isinstance(launch, Launch):
        msg = f"the waiting request must be admitted, got {launch!r}"
        raise TypeError(msg)
    return launch


def test_a_withheld_permission_launches_the_group_directly() -> None:
    """A token admitted under a recovery hold compiles without the cache."""
    admission, effects = _composed([False])

    launch = effects[0]
    assert isinstance(launch, Launch), "the token still admits the waiting request"
    assert launch.cached is False, "a withheld permission composes no cache"
    assert admission.groups["g1"].cached is False, (
        "the group's own record agrees with what was launched"
    )


def test_a_group_launched_directly_never_awaits_server_accounting() -> None:
    """A launch that ran uncached returns its token on the scope emptying.

    The four clauses of server-drain accounting are all satisfied here — the
    group is unknown-exit and cached, which is what retains a token — so the
    only thing keeping the token out of the return is the composition decided
    at the launch. If the launch and the group could disagree, this is the
    token that would be stranded until an operator stopped the cache.
    """
    admission, _ = _composed([False])
    admission.groups["g1"].cancelled = True

    effects = admission.group_emptied("g1")
    assert ReturnToken(b"+") in effects, "an uncached group accounts for no server work"


def test_a_withheld_request_that_ran_cached_still_awaits_accounting() -> None:
    """The same clauses retain the token when the launch really did use the cache."""
    admission, effects = _composed([True])
    launch = effects[0]
    assert isinstance(launch, Launch), "the permitted launch still admits the request"
    assert launch.cached is True, "a granted permission composes the managed cache"
    admission.groups["g1"].cancelled = True

    effects = admission.group_emptied("g1")
    assert ReturnToken(b"+") not in effects, (
        "a cancelled cached group retains its token"
    )
    assert admission.groups["g1"].state is GroupState.SERVER_DRAIN, (
        "the token waits on the server's own accounting"
    )


def test_permission_is_read_at_the_launch_not_at_the_request() -> None:
    """A recovery that starts while a token is in flight is not raced by it."""
    permission = [True]
    admission = Admission(cached=True, cache_allowed=lambda: permission[0])
    admission.submit(1, cacheable=True)

    # The recovery opens after the request is queued and before the pool's
    # token is read, which is the ordering the supervisor cannot prevent.
    permission[0] = False
    launch = _launched(admission)
    assert launch.cached is False, "the permission was read where the decision was"


def test_a_request_that_may_not_be_cached_ignores_the_permission() -> None:
    """A rustdoc invocation compiles directly however the cache is doing."""
    admission = Admission(cached=True, cache_allowed=lambda: True)
    admission.submit(1, cacheable=False)

    launch = _launched(admission)
    assert launch.cached is False, "an uncacheable request never composes the cache"


def test_an_unwired_admission_state_keeps_its_plain_cached_reading() -> None:
    """Admission built without a permission port behaves exactly as before."""
    admission = Admission(cached=True)
    admission.submit(1, cacheable=True)

    launch = _launched(admission)
    assert launch.cached is True, "no permission port means the reading stands alone"


def test_a_clean_exit_leaves_composition_decisions_to_server_accounting() -> None:
    """The permission changes composition only; a clean cached exit still returns."""
    admission = Admission(cached=True, cache_allowed=lambda: True)
    admission.submit(1, cacheable=True)
    admission.token_arrived(b"+")
    admission.leader_exited("g1", Exit(code=0))

    effects = admission.group_emptied("g1")
    assert ReturnToken(b"+") in effects, "a clean cached exit returns without a probe"
