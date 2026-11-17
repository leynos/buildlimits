"""When the supervisor folds a queue change relative to blocking work.

``token_arrived`` removes the request from the queue and the real registry then
blocks in the systemd call that creates the compiler scope. The depth has to be
folded at that removal, before placement starts; otherwise the open interval
keeps integrating the depth the supervisor has already left behind, and the
published queue mean grows with however long systemd takes.
"""

import types
import typing as typ

from groups_helpers import FakeGroups
from request_helpers import crate_pending
from supervisor_helpers import one_slot_supervisor

from buildlimits import supervisor_observability

if typ.TYPE_CHECKING:
    from buildlimits.groups import Groups


def test_a_blocked_placement_cannot_extend_the_queue_integral() -> None:
    """Depth is folded when a token dequeues a request, before placement runs.

    The request waits ten seconds. Placement then spends five seconds inside
    the scope call, which must not add queue-seconds: only the ten seconds the
    request actually spent waiting may reach the published mean.
    """
    queued_when_placement_blocked: list[int] = []

    with one_slot_supervisor() as scenario:
        supervisor = scenario.supervisor

        def _block_on_the_scope_call() -> None:
            """Model the systemd call a real placement waits for."""
            queued_when_placement_blocked.append(supervisor.admission.counts().queued)
            scenario.clock.time = 15.0

        supervisor.groups = typ.cast(
            "Groups",
            FakeGroups(
                order=[],
                placed=types.SimpleNamespace(exec_error=None),
                on_place=_block_on_the_scope_call,
            ),
        )
        supervisor.requests.pending[1] = crate_pending(1)
        supervisor._apply(supervisor.admission.submit(1, cacheable=False))
        scenario.clock.time = 10.0
        supervisor._apply(supervisor.admission.token_arrived(b"a"))
        supervisor_observability.flush(supervisor, force=True)

    gauges = [
        record
        for record in scenario.sink.records
        if record["BUILD_LIMITS_EVENT"] == "gauges"
    ]
    assert queued_when_placement_blocked == [0], (
        "placement starts only after the queue has already changed"
    )
    assert gauges[-1]["BUILD_LIMITS_QUEUED_SECONDS"] == "10.000", (
        "only the ten seconds the request waited count towards the mean"
    )
    assert gauges[-1]["BUILD_LIMITS_INTERVAL_SECONDS"] == "15.000", (
        "the interval still covers the whole placement"
    )
    assert any(
        record["BUILD_LIMITS_EVENT"] == "request_admitted"
        for record in scenario.sink.records
    ), "the scenario must reach a successful placement"
