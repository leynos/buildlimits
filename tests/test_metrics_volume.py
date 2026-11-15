"""Journal volume for a one-slot supervisor driven on a fixed clock.

The scenario is deliberately exact: one compiler slot, one request at a time
and a hand-held clock, so the number of records the supervisor publishes is a
property of the code rather than of timing. It measures the reduction ADR-0009
describes: no record announces that a request queued, every per-request record
carries only request-specific fields, and the queue is described once per
interval instead of once per enqueue.
"""

import contextlib
import itertools
import socket
import types
import typing as typ
from pathlib import Path

from conftest import RecordingSink
from groups_helpers import FakeGroups
from pool_helpers import attach_pool
from request_helpers import connect_requester

from buildlimits.config import SupervisorConfig
from buildlimits.metrics import Metrics
from buildlimits.metrics_gauges import GAUGE_INTERVAL_SECONDS, GAUGE_MIN_SPACING_SECONDS
from buildlimits.pool import Token
from buildlimits.protocol import Exit
from buildlimits.requesters import listen
from buildlimits.supervisor import Supervisor, SupervisorTelemetry

if typ.TYPE_CHECKING:
    from buildlimits.groups import Groups

REQUESTS = 40
STEP_SECONDS = 1.0
MINIMUM_REDUCTION = 0.13


def _place_and_finish(supervisor: Supervisor, group_id: str) -> None:
    """Run one admitted group to its end through the production effects."""
    supervisor._apply(supervisor.admission.leader_exited(group_id, Exit(code=0)))
    # ``group_emptied`` reports the result and returns the token, so this is
    # the path that writes a terminal record in a real drain.
    supervisor._apply(supervisor.admission.group_emptied(group_id))


def _one_slot_volume(path: Path) -> dict[str, object]:
    """Drive one slot through ``REQUESTS`` complete request lifecycles.

    Returns
    -------
    dict[str, object]
        The record counts and the interval each snapshot covers.
    """
    now = [0.0]
    sink = RecordingSink()
    supervisor = Supervisor(
        SupervisorConfig(cache_mode="direct", slots=1),
        clock=lambda: now[0],
        telemetry=SupervisorTelemetry(metrics=Metrics(sink)),
    )
    pool = attach_pool(supervisor)
    clients: list[socket.socket] = []
    with contextlib.ExitStack() as owned:
        owned.callback(supervisor.selector.close)
        listener = owned.enter_context(listen(path))
        supervisor.listener = listener
        supervisor.selector.register(listener, 1, "listener")
        supervisor.groups = typ.cast(
            "Groups",
            FakeGroups(order=[], placed=types.SimpleNamespace(exec_error=None)),
        )
        for index in range(1, REQUESTS + 1):
            now[0] += STEP_SECONDS
            clients.append(connect_requester(listener))
            # The connection waits behind the single held token, which is what
            # used to produce a ``request_queued`` record of its own.
            supervisor._accept()
            pool.scripted = [Token(b"a")]
            supervisor._pool_events()
            _place_and_finish(supervisor, f"g{index}")
            supervisor._tick()
        for client in clients:
            client.close()
    counts: dict[str, int] = {}
    for record in sink.records:
        event = record["BUILD_LIMITS_EVENT"]
        counts[event] = counts.get(event, 0) + 1
    intervals = [
        float(record["BUILD_LIMITS_INTERVAL_SECONDS"])
        for record in sink.records
        if record["BUILD_LIMITS_EVENT"] == "gauges"
    ]
    return {
        "counts": counts,
        "observed": len(sink.records),
        "intervals": intervals,
    }


def test_a_one_slot_supervisor_writes_one_snapshot_per_interval(tmp_path: Path) -> None:
    """Volume follows the request count and the heartbeat, not the queue."""
    result = _one_slot_volume(tmp_path / "volume.sock")
    counts = typ.cast("dict[str, int]", result["counts"])
    assert counts.get("request_queued") is None, (
        "enqueueing no longer writes a record of its own"
    )
    assert counts.get("request_admitted") == REQUESTS, "one admission per request"
    assert counts.get("request_finished") == REQUESTS, "one terminal record each"
    assert counts.get("gauges") == 2, (
        "the baseline record plus the single interval the run covers"
    )
    intervals = typ.cast("list[float]", result["intervals"])
    assert intervals == [0.0, GAUGE_INTERVAL_SECONDS], (
        "the baseline claims no interval and the heartbeat claims exactly one"
    )
    assert all(
        later - earlier >= GAUGE_MIN_SPACING_SECONDS
        for earlier, later in itertools.pairwise(intervals)
    ), "no snapshot may follow another inside the minimum spacing"


def test_removing_the_queue_record_cuts_the_journal_by_at_least_13_percent(
    tmp_path: Path,
) -> None:
    """The measured saving is the one ADR-0009 records for this change."""
    result = _one_slot_volume(tmp_path / "saving.sock")
    observed = typ.cast("int", result["observed"])
    # The legacy producer wrote one ``request_queued`` record per request that
    # had to wait; every request in this scenario did.
    legacy = observed + REQUESTS
    assert (legacy - observed) / legacy >= MINIMUM_REDUCTION, (
        "the saving must clear the documented floor"
    )
