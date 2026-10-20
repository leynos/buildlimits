"""A closing admission finishes what runs, launches nothing and refuses the rest.

Closing is the graceful half of a restart: the supervisor keeps every running
group and its token, stops asking the pool for more, and only when the last
group has emptied tells whatever still waits to try again. Nothing is ever
terminated, so nothing admitted is lost.
"""

from buildlimits.admission import (
    Admission,
    GroupState,
    NotifyFinished,
    Refuse,
    RequestToken,
    ReturnToken,
    Shutdown,
    Terminate,
)
from buildlimits.effects import Exit


def _running(admission: Admission, *request_ids: int) -> None:
    """Admit each request in turn so that one group per request holds a token."""
    for request_id in request_ids:
        admission.submit(request_id)
        admission.token_arrived(b"+")


def test_closing_an_idle_admission_shuts_down_once() -> None:
    """An idle admission emits shutdown once, even when closed again."""
    admission = Admission()
    assert admission.begin_closing() == [Shutdown()], (
        "nothing runs, so nothing is left to wait for"
    )
    assert admission.begin_closing() == [], "a repeated close has no new effects"


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


def test_a_closing_group_can_empty_before_its_leader_exit_is_reaped() -> None:
    """An early empty scope returns its token before refusing queued work."""
    admission = Admission()
    _running(admission, 1)
    admission.submit(2)
    admission.begin_closing()

    effects = admission.group_emptied("g1")
    expected = [
        ReturnToken(b"+"),
        NotifyFinished(1, Exit()),
        Refuse(2, "restarting"),
        Shutdown(),
    ]
    assert effects == expected, (
        "the empty scope is accounted without waiting for a reaped leader"
    )
    assert not admission.groups, "the empty group no longer holds its token"


def test_requester_cancellation_during_closing_still_terminates_its_group() -> None:
    """A caller disconnect can terminate its group while graceful close waits."""
    admission = Admission()
    _running(admission, 1)
    admission.begin_closing()

    assert admission.cancel(1) == [Terminate("g1")], (
        "requester cancellation, rather than closing, terminates the group"
    )
    assert admission.groups["g1"].cancelled, "the group records its cancellation"


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


def test_a_launch_that_fails_while_closing_is_refused_as_restarting() -> None:
    """Nothing ran for the request, so its adapter is told to retry it."""
    admission = Admission()
    _running(admission, 1)
    admission.begin_closing()
    assert admission.launch_failed("g1") == [
        ReturnToken(b"+"),
        Refuse(1, "restarting"),
        Shutdown(),
    ], "the token returns, the request retries, and the drained supervisor exits"
    assert not admission.groups, "the failed group is gone"


def test_a_launch_that_fails_with_no_stop_stays_a_launch_failure() -> None:
    """Only a stop in progress turns a failed launch into a retry."""
    admission = Admission()
    _running(admission, 1)
    assert admission.launch_failed("g1") == [
        ReturnToken(b"+"),
        Refuse(1, "launch-failed"),
    ], "a genuine launch failure is still reported as one"
