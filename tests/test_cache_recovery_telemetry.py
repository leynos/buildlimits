"""The recovery transaction's counters are produced by real transitions.

Every assertion here reads a record the production state machine emitted while
being driven through a real phase change; none calls ``emit`` directly. The
distinction matters because a counter can be declared, documented and never
reached — the four ``recovery_*`` counters existed with emission sites and a
schema entry but no test asserted any of them, so nothing established that a
transition actually produced them.

The machine is built over the shared scripted manager, so the phases advance on
scripted answers rather than on timing. Faults are injected the way the machine
meets them in production: a refused control job comes from the scripted
manager, and a barrier that cannot be moved comes from a guarded path method.
A transport fault is deliberately *not* used to drive a retry, because the
starvation policy refuses to open a transaction at all while the manager cannot
be read, and such a test would assert counters no transition produced. No
assertion treats elapsed time, idle CPU or a missing compiler process as
accounting proof.
"""

import typing as typ
from pathlib import Path

from cache_recovery_barrier_helpers import unremovable_barrier
from cache_recovery_harness import metered_machine
from cache_recovery_helpers import MACHINE_STEP_SECONDS, gauges, settle_transaction

from buildlimits import cache_recovery

if typ.TYPE_CHECKING:
    import pytest
    from cache_recovery_helpers import CacheState
    from conftest import RecordingSink

    from buildlimits.admission import Counts
    from buildlimits.metrics import Metrics

# A snapshot where one token awaits server accounting and one request waits:
# the exact conjunction the automatic trigger acts on.
STARVING = gauges(queued=1, server_drain=1)


def _records(sink: RecordingSink, counter: str) -> list[dict[str, str]]:
    """Select the cache-recovery records carrying one counter, in order."""
    key = f"BUILD_LIMITS_{counter.upper()}"
    return [
        record
        for record in sink.records
        if record.get("BUILD_LIMITS_EVENT") == "cache_recovery" and key in record
    ]


def _counted(sink: RecordingSink, counter: str) -> int:
    """Sum the values recorded for one counter."""
    key = f"BUILD_LIMITS_{counter.upper()}"
    return sum(int(record[key]) for record in _records(sink, counter))


def _drive(
    machine: cache_recovery.CacheRecovery, ticks: int, start: float = 0.0
) -> float:
    """Tick a transaction, returning the instant it last ticked at."""
    now = start
    for _ in range(ticks):
        now += MACHINE_STEP_SECONDS
        machine.tick(now)
    return now


def _clear_refuses_once(
    monkeypatch: pytest.MonkeyPatch, marker: Path, fault: str = "Stale file handle"
) -> None:
    """Fail the guarded barrier's first removal, then let the next one through."""
    unlink = Path.unlink
    remaining = [1]

    def refuse_once(self: Path, *, missing_ok: bool = False) -> None:
        """Refuse the guarded barrier's removal exactly once.

        Raises
        ------
        OSError
            For the guarded barrier's first removal only, standing in for a
            runtime directory that was briefly unwritable.
        """
        if self == marker and remaining[0]:
            remaining[0] = 0
            raise OSError(fault)
        unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_once)


def _requested(
    tmp_path: Path, metrics: Metrics, *, refuse: str | None = None
) -> tuple[cache_recovery.CacheRecovery, CacheState, list[Counts]]:
    """Open a manual transaction over a starving reading.

    Returns
    -------
    tuple[cache_recovery.CacheRecovery, CacheState, list[Counts]]
        The machine, its scripted cache with ``refuse`` applied, and the gauge
        cell the transaction reads.
    """
    snapshot = [STARVING]
    machine, state = metered_machine(tmp_path, metrics, snapshot=snapshot)
    state.refuse = refuse
    machine.request("manual", 0.0)
    return machine, state, snapshot


def _assert_one_restart_failure(sink: RecordingSink) -> None:
    """Check an exhausted restart counted one failure and one completion."""
    failures = _records(sink, "recovery_failures")
    assert len(failures) == 1, "an exhausted transaction counts one failure"
    assert failures[0]["BUILD_LIMITS_PHASE"] == "restarting", (
        "the failure is attributed to the restart step"
    )
    # The value as well as the record: a counter emitted as zero would satisfy
    # a presence check while counting nothing.
    assert _counted(sink, "recovery_failures") == 1, (
        "the failure counter carries the value it reports"
    )
    assert _counted(sink, "recovery_completions") == 1, (
        "a faulted ending is its own terminal count, not a silent absence"
    )


def test_attempts_count_once_per_failed_action_including_repeats(
    tmp_path: Path, metrics: Metrics, metric_sink: RecordingSink
) -> None:
    """A retry that repeats its reason is counted, not deduplicated."""
    # A manager that refuses the stop fails the step on every attempt, with
    # identical text each time. The refusal is a refused job, not a transport
    # fault: a faulted transport blocks the transaction from opening at all.
    machine, state, _ = _requested(tmp_path, metrics, refuse="stop")
    _drive(machine, 4)

    attempts = _records(metric_sink, "recovery_attempts")
    assert len(attempts) == 3, "one attempt record per failed action"
    assert all(record["BUILD_LIMITS_PHASE"] == "stopping" for record in attempts), (
        "every recorded attempt names the phase that failed"
    )
    # The deduplication the journal line applies must not reach the counter.
    assert machine.describe(0.0)["attempts"] == 3, (
        "the status document counts the same failures the records do"
    )
    assert state.last_attempted("stop"), "the stop was refused, not skipped"
    assert "start" not in state.verbs, "a refused stop never reaches the restart"


def test_exhaustion_emits_once_at_the_failure_limit(
    tmp_path: Path, metrics: Metrics, metric_sink: RecordingSink
) -> None:
    """Exhaustion is counted once, and a refused stop keeps retrying."""
    machine, _, _ = _requested(tmp_path, metrics, refuse="stop")
    _drive(machine, cache_recovery.RECOVERY_FAILURE_LIMIT + 3)

    exhaustions = _records(metric_sink, "recovery_exhaustions")
    assert len(exhaustions) == 1, "a transaction exhausts at most once"
    assert exhaustions[0]["BUILD_LIMITS_PHASE"] == "stopping", (
        "exhaustion names the phase that gave up"
    )
    # The value as well as the record: an emission stamped two would still be
    # one record, so a count-of-records check alone would let a double-counted
    # exhaust through. Asserting on the value closes that gap.
    assert _counted(metric_sink, "recovery_exhaustions") == 1, (
        "the exhaustion counter carries the value it reports"
    )
    assert _counted(metric_sink, "recovery_attempts") == (
        cache_recovery.RECOVERY_FAILURE_LIMIT + 2
    ), "the count keeps rising past the limit that stopped reporting"
    # The limit bounds the reporting, never the retrying: a stop that was not
    # accepted leaves the cache live, and its token must not be released.
    assert machine.describe(0.0)["phase"] == "stopping", (
        "a refused stop never finishes the transaction"
    )
    assert _counted(metric_sink, "recovery_completions") == 0, (
        "no token may be released while the cache is still live"
    )


def test_completion_emits_once_and_does_not_repeat_while_idle(
    tmp_path: Path, metrics: Metrics, metric_sink: RecordingSink
) -> None:
    """A clean transaction completes once and stays quiet afterwards."""
    snapshot = [STARVING]
    machine, _ = metered_machine(tmp_path, metrics, snapshot=snapshot)

    # No explicit request: the first tick opens the transaction by policy.
    now = settle_transaction(machine, snapshot)
    assert machine.describe(now)["phase"] == "idle", "the transaction completed"

    assert _counted(metric_sink, "recovery_completions") == 1, (
        "one clean transaction counts one completion"
    )
    assert _counted(metric_sink, "recovery_failures") == 0, "a clean run fails nothing"

    # Idle ticks must not re-count a transaction that already ended.
    _drive(machine, 5, start=now)
    assert _counted(metric_sink, "recovery_completions") == 1, (
        "an idle transaction is not counted a second time"
    )
    assert _counted(metric_sink, "recovery_attempts") == 0, (
        "idle ticks take no action, so they record no attempt"
    )


def test_completion_after_retries_still_counts_one_transaction(
    tmp_path: Path,
    metrics: Metrics,
    metric_sink: RecordingSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient fault and a later success share one completion."""
    machine, state, snapshot = _requested(tmp_path, metrics)
    # The barrier's first removal fails; the retry of that same action clears it.
    _clear_refuses_once(monkeypatch, machine.barrier.path)

    now = settle_transaction(machine, snapshot)

    assert machine.describe(now)["phase"] == "idle", (
        "a recovered fault still reaches a clean ending"
    )
    assert _counted(metric_sink, "recovery_attempts") == 1, (
        "the barrier fault was counted"
    )
    assert _counted(metric_sink, "recovery_exhaustions") == 0, (
        "one fault is under the limit"
    )
    assert _counted(metric_sink, "recovery_completions") == 1, (
        "one transaction counts one completion however many attempts it took"
    )
    assert state.verbs == ["stop", "start"], (
        "the transaction still stopped and restarted"
    )
    assert machine.describe(now)["last_fault"] is None, (
        "a recovered fault is retired rather than left visible"
    )


def test_a_refused_restart_counts_a_failure_and_still_completes(
    tmp_path: Path, metrics: Metrics, metric_sink: RecordingSink
) -> None:
    """An exhausted restart is its own terminal count, not a silent absence."""
    machine, state, snapshot = _requested(tmp_path, metrics, refuse="start")

    now = settle_transaction(
        machine, snapshot, ticks=cache_recovery.RECOVERY_FAILURE_LIMIT * 2 + 4
    )

    _assert_one_restart_failure(metric_sink)
    assert (
        _counted(metric_sink, "recovery_attempts")
        == cache_recovery.RECOVERY_FAILURE_LIMIT
    ), "the limit is the number of attempts actually taken"
    assert machine.describe(now)["last_fault"] is not None, "the fault stays visible"
    assert state.verbs[0] == "stop", "the accounting stop still happened"


def test_a_barrier_that_cannot_be_removed_counts_a_failure_and_still_completes(
    tmp_path: Path,
    metrics: Metrics,
    metric_sink: RecordingSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unremovable barrier exhausts the restart step like a refused start."""
    machine, state, snapshot = _requested(tmp_path, metrics)
    unremovable_barrier(monkeypatch, machine.barrier.path, "Read-only file system")

    now = settle_transaction(
        machine, snapshot, ticks=cache_recovery.RECOVERY_FAILURE_LIMIT * 2 + 4
    )

    _assert_one_restart_failure(metric_sink)
    # The barrier is what the unit's own condition reads, so no start may be
    # queued behind one that survived: the fault names the barrier, not the
    # manager's refusal of a job it was never asked to run.
    assert "cannot clear" in str(machine.describe(now)["last_fault"]), (
        "the fault names the step that actually failed"
    )
    assert state.verbs == ["stop"], "no start was asked for behind the barrier"


def test_starved_policy_open_and_close_are_both_counted(
    tmp_path: Path, metrics: Metrics, metric_sink: RecordingSink
) -> None:
    """The automatic trigger emits the same counters as an operator request."""
    snapshot = [STARVING]
    machine, _ = metered_machine(tmp_path, metrics, snapshot=snapshot)

    # No explicit request: the first tick opens the transaction by policy.
    machine.tick(MACHINE_STEP_SECONDS)
    assert machine.describe(MACHINE_STEP_SECONDS)["phase"] != "idle", (
        "starvation opened it"
    )

    now = settle_transaction(machine, snapshot, start=MACHINE_STEP_SECONDS)
    assert machine.describe(now)["phase"] == "idle", (
        "the automatic transaction completed"
    )
    assert _counted(metric_sink, "recovery_completions") == 1, (
        "the automatic trigger counts its completion like a manual one"
    )
    phases = [
        record["BUILD_LIMITS_PHASE"]
        for record in metric_sink.records
        if record.get("BUILD_LIMITS_EVENT") == "cache_recovery"
    ]
    assert "quiescing" in phases, "each phase transition is observable"
    assert "stopping" in phases, "the stop it performed is observable"
