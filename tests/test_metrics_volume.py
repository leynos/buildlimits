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
import types
import typing as typ
from pathlib import Path

from groups_helpers import FakeGroups
from request_helpers import connect_requester
from supervisor_helpers import one_slot_supervisor

from buildlimits.metrics_gauges import GAUGE_INTERVAL_SECONDS, GAUGE_MIN_SPACING_SECONDS
from buildlimits.pool import Token
from buildlimits.protocol import Exit
from buildlimits.requesters import listen

if typ.TYPE_CHECKING:
    from buildlimits.groups import Groups
    from buildlimits.supervisor import Supervisor

REQUESTS = 40
STEP_SECONDS = 1.0
MINIMUM_REDUCTION = 0.13

# The representative load recorded for #56: one busy hour of the supervisor's
# own journal after #55 removed the adapters' records. 35,094 of its 104,457
# records announced an enqueue, so removing that event saves about a third of
# the supervisor's journal rather than the one record per request the exact
# one-slot scenario happens to produce.
BUSY_HOUR_SECONDS = 3600.0
BUSY_HOUR_RECORDS = 104_457
BUSY_HOUR_QUEUED_RECORDS = 35_094


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
    with one_slot_supervisor() as scenario, contextlib.ExitStack() as owned:
        supervisor = scenario.supervisor
        listener = owned.enter_context(listen(path))
        supervisor.listener = listener
        supervisor.selector.register(listener, 1, "listener")
        supervisor.groups = typ.cast(
            "Groups",
            FakeGroups(order=[], placed=types.SimpleNamespace(exec_error=None)),
        )
        for index in range(1, REQUESTS + 1):
            scenario.clock.advance(STEP_SECONDS)
            owned.callback(connect_requester(listener).close)
            # The connection waits behind the single held token, which is what
            # used to produce a ``request_queued`` record of its own.
            supervisor._accept()
            scenario.pool.scripted = [Token(b"a")]
            supervisor._pool_events()
            _place_and_finish(supervisor, f"g{index}")
            supervisor._tick()
    counts: dict[str, int] = {}
    for record in scenario.sink.records:
        event = record["BUILD_LIMITS_EVENT"]
        counts[event] = counts.get(event, 0) + 1
    intervals = [
        float(record["BUILD_LIMITS_INTERVAL_SECONDS"])
        for record in scenario.sink.records
        if record["BUILD_LIMITS_EVENT"] == "gauges"
    ]
    return {
        "counts": counts,
        "observed": len(scenario.sink.records),
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
    counts = typ.cast("dict[str, int]", result["counts"])
    observed = typ.cast("int", result["observed"])
    # Both producers write the same per-request records. The snapshot is new,
    # so it belongs to the new count only; every waiting request cost the
    # legacy producer one extra ``request_queued`` record.
    snapshots = counts.get("gauges", 0)
    legacy = observed - snapshots + REQUESTS
    assert (legacy - observed) / legacy >= MINIMUM_REDUCTION, (
        "the saving must clear the documented floor"
    )


def test_the_documented_floor_holds_for_a_representative_busy_hour() -> None:
    """The recorded busy hour saves more than the one-slot scenario promises."""
    # The new producer pays for the snapshots it adds. The minimum spacing
    # caps every trigger, heartbeat or step, so this is the most it can write
    # in the hour rather than an estimate: seven hundred-odd records against
    # the tens of thousands of enqueue records the same hour used to carry.
    snapshots = int(BUSY_HOUR_SECONDS // GAUGE_MIN_SPACING_SECONDS) + 1
    after = BUSY_HOUR_RECORDS - BUSY_HOUR_QUEUED_RECORDS + snapshots
    reduction = (BUSY_HOUR_RECORDS - after) / BUSY_HOUR_RECORDS
    assert reduction >= MINIMUM_REDUCTION, (
        "the representative load keeps the documented saving a lower bound"
    )
