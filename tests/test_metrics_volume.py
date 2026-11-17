"""Journal volume measured from the real producers at two loads.

The exact scenario drives one compiler slot through complete request lifecycles
on a hand-held clock, so the record count follows the code rather than timing.
The representative scenario runs a busy-hour shape through the same admission,
port and accumulator, and reads the resulting journal back through the real
query and report.

Every legacy count in this module comes from driving the real validating
emitter over the events the same run produced, plus one ``request_queued``
record for each request the run measured as waiting. No figure is derived by
subtracting module constants.
"""

import contextlib
import itertools
import types
import typing as typ
from pathlib import Path

import pytest
from conftest import RecordingSink
from groups_helpers import FakeGroups
from journal_fixtures import journal_lines, journal_runner
from request_helpers import connect_requester
from supervisor_helpers import OneSlotScenario, one_slot_supervisor

from buildlimits.metrics import Metrics
from buildlimits.metrics_gauges import GAUGE_INTERVAL_SECONDS, GAUGE_MIN_SPACING_SECONDS
from buildlimits.metrics_query import query
from buildlimits.metrics_report import report
from buildlimits.pool import Token
from buildlimits.protocol import Exit
from buildlimits.requesters import listen

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import socket

    from buildlimits.groups import Groups
    from buildlimits.supervisor import Supervisor

REQUESTS = 40
BUSY_HOUR_REQUESTS = 3_600
STEP_SECONDS = 1.0
MINIMUM_REDUCTION = 0.13


class _Volume:
    """What one run wrote and what the legacy producer would have written.

    Attributes
    ----------
    records : list[dict[str, str]]
        Every validated record the run's sink accepted, in order.
    counts : dict[str, int]
        Records per event name.
    waiting : int
        Requests the run measured as queued when admission took them.
    """

    def __init__(self, records: list[dict[str, str]], waiting: int) -> None:
        """Keep the run's records and the measured queue count."""
        self.records = records
        self.counts: dict[str, int] = {}
        for record in records:
            event = record["BUILD_LIMITS_EVENT"]
            self.counts[event] = self.counts.get(event, 0) + 1
        self.waiting = waiting

    def legacy_records(self) -> int:
        """Write the same load through the legacy producer and count records.

        The legacy producer wrote every event this run wrote, except the
        periodic ``gauges`` snapshot, which did not exist. It also wrote one
        ``request_queued`` record for each request the run measured as waiting.

        Returns
        -------
        int
            Records the legacy producer accepted.
        """
        sink = RecordingSink()
        emitter = Metrics(sink)
        for event in (record["BUILD_LIMITS_EVENT"] for record in self.records):
            if event == "gauges":
                continue
            assert emitter.emit(event), f"the legacy producer accepted {event}"
        for _ in range(self.waiting):
            assert emitter.emit("request_queued"), "one enqueue record per wait"
        return len(sink.records)

    def reduction(self) -> float:
        """Report the fractional fall from the measured legacy count.

        Returns
        -------
        float
            The share of legacy records the new producer does not write.
        """
        legacy = self.legacy_records()
        return (legacy - len(self.records)) / legacy


def _place_and_finish(supervisor: Supervisor, group_id: str) -> None:
    """Run one admitted group to its end through the production effects."""
    supervisor._apply(supervisor.admission.leader_exited(group_id, Exit(code=0)))
    # ``group_emptied`` reports the result and returns the token, so this is
    # the path that writes a terminal record in a real drain.
    supervisor._apply(supervisor.admission.group_emptied(group_id))


def _release_and_place(scenario: OneSlotScenario, group_id: str) -> None:
    """Return the pool's token and run the group it admits to its end.

    Parameters
    ----------
    scenario : OneSlotScenario
        The run whose single token returns.
    group_id : str
        The group the placement registered.
    """
    scenario.pool.scripted = [Token(b"a")]
    scenario.supervisor._pool_events()
    _place_and_finish(scenario.supervisor, group_id)


@contextlib.contextmanager
def _listening(path: Path) -> cabc.Generator[tuple[OneSlotScenario, socket.socket]]:
    """Run a one-slot supervisor whose listener accepts real connections.

    Driving the real listener means every group the placement registers is a
    real one, so the run reads the same journal the report reads.

    Parameters
    ----------
    path : Path
        Where to bind the supervisor's listening socket.

    Yields
    ------
    tuple[OneSlotScenario, socket.socket]
        The scenario and the listening socket requesters connect to.
    """
    with one_slot_supervisor() as scenario:
        supervisor = scenario.supervisor
        with listen(path) as listener:
            supervisor.listener = listener
            supervisor.selector.register(listener, 1, "listener")
            supervisor.groups = typ.cast(
                "Groups",
                FakeGroups(order=[], placed=types.SimpleNamespace(exec_error=None)),
            )
            yield scenario, listener


def _volume(path: Path, requests: int) -> _Volume:
    """Run ``requests`` complete request lifecycles through the real producers.

    Each request arrives at a whole second and is accepted while the token the
    previous one took is still out, so it waits for one second; the token
    returns as the next request arrives, which is the shape a busy hour of
    compilations has.

    Parameters
    ----------
    path : Path
        Where to bind the supervisor's listening socket.
    requests : int
        How many request lifecycles to run.

    Returns
    -------
    _Volume
        The records the run wrote and the requests that waited.
    """
    waiting = 0
    with _listening(path) as (scenario, listener):
        supervisor = scenario.supervisor
        for index in range(1, requests + 1):
            with contextlib.closing(connect_requester(listener)):
                # The queue is one deep, which is what used to produce a
                # ``request_queued`` record of its own.
                supervisor._accept()
                waiting += bool(supervisor.admission.counts().queued)
                supervisor._tick()
                scenario.clock.advance(STEP_SECONDS)
                _release_and_place(scenario, f"g{index}")
        supervisor._tick()
        records = [dict(record) for record in scenario.sink.records]
    return _Volume(records, waiting)


def _gauges(records: cabc.Sequence[dict[str, str]]) -> list[dict[str, str]]:
    """Select the periodic snapshot records.

    Returns
    -------
    list[dict[str, str]]
        The ``gauges`` records in the order they were written.
    """
    return [record for record in records if record["BUILD_LIMITS_EVENT"] == "gauges"]


def test_a_one_slot_supervisor_writes_one_snapshot_per_interval(
    tmp_path: Path,
) -> None:
    """Volume follows the request count and the heartbeat, not the queue."""
    volume = _volume(tmp_path / "volume.sock", REQUESTS)
    assert volume.counts.get("request_queued") is None, (
        "enqueueing no longer writes a record of its own"
    )
    assert volume.counts.get("request_admitted") == REQUESTS, (
        "one admission per request"
    )
    assert volume.counts.get("request_finished") == REQUESTS, "one terminal record each"
    assert volume.counts.get("gauges") == 2, (
        "the baseline record plus the single interval the run covers"
    )
    intervals = [
        float(record["BUILD_LIMITS_INTERVAL_SECONDS"])
        for record in _gauges(volume.records)
    ]
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
    """The saving is measured from both producers over the same load."""
    volume = _volume(tmp_path / "saving.sock", REQUESTS)
    assert volume.waiting == REQUESTS, "every request in the run waited its turn"
    assert volume.reduction() >= MINIMUM_REDUCTION, (
        "the measured saving must clear the documented floor"
    )


def test_a_busy_hour_keeps_the_floor_and_a_time_weighted_mean(
    tmp_path: Path,
) -> None:
    """A representative load is measured, then read back by the real report."""
    volume = _volume(tmp_path / "busy.sock", BUSY_HOUR_REQUESTS)
    assert volume.waiting == BUSY_HOUR_REQUESTS, "every request waited one second"
    assert volume.reduction() >= MINIMUM_REDUCTION, (
        "the one-slot measurement is not an artefact of a short run"
    )
    snapshots = _gauges(volume.records)
    assert len(snapshots) == 1 + int(
        BUSY_HOUR_REQUESTS * STEP_SECONDS // GAUGE_INTERVAL_SECONDS
    ), "the hour holds its baseline plus one snapshot per heartbeat"
    window = journal_lines(
        (record, index) for index, record in enumerate(volume.records)
    )
    gauges = typ.cast(
        "dict[str, object]", report(query(runner=journal_runner(window)))["gauges"]
    )
    assert gauges["queue_time_weighted_mean"] == pytest.approx(1.0), (
        "one request waited for every second of the hour"
    )
    assert gauges["queue_maximum"] == pytest.approx(1.0), (
        "the queue never exceeded one request"
    )
    assert gauges["held_maximum"] == pytest.approx(1.0), (
        "the interval peak keeps the held token the boundaries did not sample"
    )
    assert gauges["gauge_coverage_seconds"] == pytest.approx(
        BUSY_HOUR_REQUESTS * STEP_SECONDS
    ), "the snapshots cover the whole hour"
