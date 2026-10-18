"""A closing admission finishes what runs, launches nothing and refuses the rest.

Closing is the graceful half of a restart: the supervisor keeps every running
group and its token, stops asking the pool for more, and only when the last
group has emptied tells whatever still waits to try again. Nothing is ever
terminated, so nothing admitted is lost.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from buildlimits.admission import (
    Admission,
    GroupState,
    Launch,
    NotifyFinished,
    Refuse,
    RequestToken,
    ReturnToken,
    Shutdown,
    Terminate,
)
from buildlimits.effects import Effect, Exit


def _running(admission: Admission, *request_ids: int) -> None:
    """Admit each request in turn so that one group per request holds a token."""
    for request_id in request_ids:
        admission.submit(request_id)
        admission.token_arrived(b"+")


def test_closing_an_idle_admission_shuts_down_at_once() -> None:
    """With no group to wait for, closing refuses nothing and exits."""
    assert Admission().begin_closing() == [Shutdown()], (
        "nothing runs, so nothing is left to wait for"
    )


def test_closing_keeps_running_groups_and_waits_for_them() -> None:
    """Closing terminates nothing and does not exit while a group holds a token."""
    admission = Admission()
    _running(admission, 1)
    assert admission.begin_closing() == [], (
        "a running group is neither touched nor left"
    )
    assert admission.groups["g1"].state is GroupState.RUNNING, "the group still runs"
    assert admission.healthy, "closing is not a health failure"


def test_a_closing_admission_queues_without_requesting_a_token() -> None:
    """New requests wait quietly: no token is requested and none is launched."""
    admission = Admission()
    _running(admission, 1)
    admission.begin_closing()
    effects = admission.submit(2)
    assert not any(isinstance(effect, RequestToken) for effect in effects), (
        "a closing admission asks the pool for nothing"
    )
    assert list(admission.queue) == [2], "the request waits for the next supervisor"


def test_a_token_already_on_its_way_is_returned_unused() -> None:
    """A token requested before closing is handed straight back to the pool."""
    admission = Admission()
    _running(admission, 1)
    admission.submit(2)
    assert admission.token_requested, "a token is on its way for the waiting request"
    admission.begin_closing()
    assert admission.token_arrived(b"-") == [ReturnToken(b"-")], (
        "no launch follows a token that arrives after closing began"
    )
    assert list(admission.queue) == [2], "the waiting request is still waiting"


def test_a_request_arriving_after_the_last_group_is_refused_at_once() -> None:
    """With nothing left to wait for, a late request is told to retry directly."""
    admission = Admission()
    admission.begin_closing()
    assert admission.submit(7) == [Refuse(7, "restarting")], (
        "queueing it would leave it to be dropped with the process"
    )


def test_the_last_group_to_empty_refuses_the_queue_then_shuts_down() -> None:
    """Waiting requests are refused as restarting, once, before the exit."""
    admission = Admission()
    _running(admission, 1)
    admission.submit(2)
    admission.submit(3)
    admission.begin_closing()
    admission.leader_exited("g1", Exit(code=0))
    expected = [
        ReturnToken(b"+"),
        NotifyFinished(1, Exit(code=0)),
        Refuse(2, "restarting"),
        Refuse(3, "restarting"),
        Shutdown(),
    ]
    assert admission.group_emptied("g1") == expected, (
        "the finished group is reported and returned before the queue is refused"
    )
    assert not admission.queue, "a refused request is not queued twice"


def test_a_server_drain_token_keeps_a_closing_admission_open() -> None:
    """A token awaiting cache accounting is not released by closing."""
    admission = Admission(cached=True)
    _running(admission, 1)
    admission.leader_exited("g1", Exit(signal=9))
    admission.begin_closing()
    assert admission.group_emptied("g1") == [NotifyFinished(1, Exit(signal=9))], (
        "the signalled cached group keeps its token and the supervisor stays up"
    )
    assert admission.server_activity(1) == [], "a live compile holds the token"
    assert admission.server_activity(0) == [ReturnToken(b"+"), Shutdown()], (
        "only the accounted stop releases the token and lets the supervisor exit"
    )


def _step(admission: Admission, operation: str, request_id: int) -> list[Effect]:
    """Apply one scripted operation, doing nothing if it does not apply.

    Returns
    -------
    list[Effect]
        The effects the operation produced.
    """
    running = _first(admission, GroupState.RUNNING)
    draining = _first(admission, GroupState.DRAINING)
    effects: list[Effect] = []
    match operation:
        case "submit":
            effects = admission.submit(request_id)
        case "token" if admission.token_requested:
            effects = admission.token_arrived(b"+")
        case "exit" if running:
            effects = admission.leader_exited(running, Exit(code=0))
        case "empty" if draining:
            effects = admission.group_emptied(draining)
        case "cancel" if admission.queue:
            effects = admission.cancel(admission.queue[0])
        case "close":
            effects = admission.begin_closing()
    return effects


@settings(max_examples=200, deadline=None)
@given(
    operations=st.lists(
        st.sampled_from(["submit", "token", "exit", "empty", "close", "cancel"]),
        max_size=40,
    )
)
def test_closing_never_launches_terminates_or_exits_early(
    operations: list[str],
) -> None:
    """Whatever happens around it, a closed admission only winds down.

    After closing begins no launch, token request or termination of a running
    group is ever produced, and the shutdown comes exactly when the last group
    is gone, with nothing left in the queue.
    """
    admission = Admission()
    shutdowns = 0
    for request_id, operation in enumerate(operations, start=1):
        was_closed = admission.closing
        effects = _step(admission, operation, request_id)
        if was_closed:
            assert not any(isinstance(e, (Launch, RequestToken)) for e in effects), (
                "a closing admission launches nothing and requests no token"
            )
            assert all(
                not isinstance(e, Terminate) or operation == "exit" for e in effects
            ), "only a leader's own exit terminates leftovers; closing cancels nothing"
        shutdowns += sum(isinstance(effect, Shutdown) for effect in effects)
        assert (shutdowns >= 1) == (admission.closing and not admission.groups), (
            "shutdown is signalled exactly when closing has nothing left to wait for"
        )
        assert not (admission.closing and not admission.groups and admission.queue), (
            "the queue is refused before the supervisor exits"
        )


def _first(admission: Admission, state: GroupState) -> str | None:
    """Pick the oldest group in ``state``, if any.

    Returns
    -------
    str | None
        Its identifier, or None when no group is in that state.
    """
    return next(
        (group.group_id for group in admission.groups.values() if group.state is state),
        None,
    )
