"""The recovery records carry the admission reading, not only the counters.

A counter answers how many times an action failed. A gauge answers what
admission looked like while it did, which is the half an operator actually
diagnoses from: a transaction that stopped the cache with work still queued
reads very differently from one that stopped an idle cache.

The two travel in the same record, so the second can be lost without the
first noticing. Every counter assertion in
:mod:`test_cache_recovery_telemetry` selects records by counter key and never
reads a gauge, so deleting ``gauge_fields`` from the two emission sites would
leave that whole module passing. This module reads the gauges back off records
the production machine emitted, and does so across a reading that changes
mid-transaction so that a record carrying the pre-stop snapshot cannot be
mistaken for one carrying the post-accounting snapshot.
"""

import typing as typ
from pathlib import Path

from cache_recovery_helpers import gauges, settle_transaction
from cache_recovery_machines import metered_machine

if typ.TYPE_CHECKING:
    from conftest import RecordingSink

    from buildlimits.metrics import Metrics

# Two readings that differ on every axis a gauge reports: one queued request
# and one token awaiting server accounting before the stop, and an entirely
# empty pool once the accounting has completed. A record carrying a constant,
# a stale reading, or the wrong moment's snapshot cannot satisfy both.
WAITING = gauges(queued=1, server_drain=1)

# Those two readings as the emitted record carries them, so each assertion
# compares against one named expectation rather than restating the six axes.
WAITING_FIELDS = {
    "queued": "1",
    "held": "1",
    "running": "0",
    "draining": "0",
    "server_drain": "1",
    "capacity": "2",
}
ACCOUNTED_FIELDS = {
    "queued": "0",
    "held": "0",
    "running": "0",
    "draining": "0",
    "server_drain": "0",
    "capacity": "2",
}


def _records(sink: RecordingSink) -> list[dict[str, str]]:
    """Select the cache-recovery records, in emission order."""
    return [
        record
        for record in sink.records
        if record.get("BUILD_LIMITS_EVENT") == "cache_recovery"
    ]


def _reading(record: dict[str, str]) -> dict[str, str]:
    """Read the six gauge fields off one record under short names."""
    return {
        axis: record[f"BUILD_LIMITS_{axis.upper()}"]
        for axis in (
            "queued",
            "held",
            "running",
            "draining",
            "server_drain",
            "capacity",
        )
    }


def test_a_transition_records_the_reading_taken_at_that_moment(
    tmp_path: Path, metrics: Metrics, metric_sink: RecordingSink
) -> None:
    """The stop's transition carries the reading that opened the transaction."""
    snapshot = [WAITING]
    machine, _ = metered_machine(tmp_path, metrics, snapshot=snapshot)

    now = settle_transaction(machine, snapshot)
    assert machine.describe(now)["phase"] == "idle", "the transaction completed"

    readings = [_reading(record) for record in _records(metric_sink)]
    assert readings, "the transaction emitted records to read gauges from"
    assert WAITING_FIELDS in readings, (
        "a transition records the reading taken when it happened, so the "
        "starving state that opened the transaction is visible in it"
    )


def test_a_completed_transaction_records_the_settled_reading(
    tmp_path: Path, metrics: Metrics, metric_sink: RecordingSink
) -> None:
    """Completion carries the post-accounting reading, not the pre-stop one."""
    snapshot = [WAITING]
    machine, state = metered_machine(tmp_path, metrics, snapshot=snapshot)
    state.refuse = ""
    machine.request("manual", 0.0)

    now = settle_transaction(machine, snapshot)
    assert machine.describe(now)["phase"] == "idle", "the transaction completed"

    completions = [
        record
        for record in _records(metric_sink)
        if record.get("BUILD_LIMITS_RECOVERY_COMPLETIONS") == "1"
    ]
    assert len(completions) == 1, "one transaction counts one completion"
    assert _reading(completions[0]) == ACCOUNTED_FIELDS, (
        "the completion reports the pool as the accounting left it: every "
        "token returned, and the configured capacity unmoved by the restart"
    )
