"""Tests for releasing server-drain tokens as the sccache server's load falls.

A cached group that ends abnormally may leave a compilation in the managed
server. Its token may return only while the server's load fits within the
server-drain tokens that remain; running and draining cached groups return
their tokens without a poll, so they never cover another group's work.
"""

from buildlimits.admission import Admission, GroupState, ReturnToken
from buildlimits.effects import Exit


def _cached_groups(count: int) -> Admission:
    """Admit ``count`` cached groups, g1 upwards, holding tokens a, b, c..."""
    admission = Admission(cached=True)
    for index in range(count):
        admission.submit(index + 1)
        admission.token_arrived(bytes([ord("a") + index]))
    return admission


def _kill(admission: Admission, *group_ids: str) -> None:
    """End groups by a signal and empty their scopes, into server drain."""
    for group_id in group_ids:
        admission.leader_exited(group_id, Exit(signal=9))
        admission.group_emptied(group_id)


def test_a_draining_cached_token_never_covers_server_work() -> None:
    """A normally exited group's token cannot stand in for a killed one's.

    g1 is killed while the server compiles for it; g2 exits normally but its
    scope has not emptied. Releasing g1's token against g2's would leave the
    server's compilation uncovered once g2's scope empties without a poll.
    """
    admission = _cached_groups(2)
    _kill(admission, "g1")
    admission.leader_exited("g2", Exit(code=0))
    assert not admission.server_activity(active_compiles=1), (
        "g1's token is held while the server compiles for it"
    )
    assert ReturnToken(b"b") in admission.group_emptied("g2"), (
        "g2's token returns when its scope empties, without a poll"
    )
    assert admission.cached_tokens_held() == 1, "g1's token still covers the server"
    assert admission.server_activity(active_compiles=0) == [ReturnToken(b"a")], (
        "g1's token returns once the server is idle"
    )


def test_server_drain_tokens_return_oldest_first_as_load_declines() -> None:
    """Each finished server compilation releases one token, oldest first."""
    admission = _cached_groups(3)
    _kill(admission, "g1", "g2", "g3")
    assert not admission.server_activity(active_compiles=3), "all three are covered"
    assert admission.server_activity(active_compiles=2) == [ReturnToken(b"a")], (
        "the oldest token returns first"
    )
    assert admission.server_activity(active_compiles=1) == [ReturnToken(b"b")], (
        "the next oldest follows"
    )
    assert admission.server_activity(active_compiles=0) == [ReturnToken(b"c")], (
        "the last returns when the server is idle"
    )


def test_several_server_drain_tokens_return_together() -> None:
    """A load that fell by several releases several tokens at once."""
    admission = _cached_groups(3)
    _kill(admission, "g1", "g2", "g3")
    assert admission.server_activity(active_compiles=1) == [
        ReturnToken(b"a"),
        ReturnToken(b"b"),
    ], "two tokens return, oldest first, leaving one for the live compilation"
    assert list(admission.groups) == ["g3"], "the newest server-drain group remains"


def test_a_running_cached_group_does_not_cover_server_work() -> None:
    """A running group's token is reserved for its own compilation."""
    admission = _cached_groups(2)
    _kill(admission, "g1")
    assert not admission.server_activity(active_compiles=1), (
        "the live compilation may be g1's, so g1's token stays"
    )
    assert admission.groups["g2"].state is GroupState.RUNNING, "g2 keeps running"
    assert admission.server_activity(active_compiles=0) == [ReturnToken(b"a")], (
        "an idle server releases g1 even while g2 runs"
    )


def test_an_idle_server_releases_despite_a_group_awaiting_emptiness() -> None:
    """With no server work, a draining neighbour does not hold anything back."""
    admission = _cached_groups(2)
    _kill(admission, "g1")
    admission.leader_exited("g2", Exit(code=0))
    assert admission.server_activity(active_compiles=0) == [ReturnToken(b"a")], (
        "g1's token returns"
    )
    assert admission.groups["g2"].state is GroupState.DRAINING, (
        "g2 still waits for its scope to empty"
    )
