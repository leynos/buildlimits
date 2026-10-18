"""The recovery machine's trigger policy, bounds and fault reporting.

These tests drive :class:`~buildlimits.cache_recovery.CacheRecovery` over
scripted gauges and a scripted manager, so the decision to open a transaction
and the way a failure is reported can be examined without running the whole
supervisor. The transaction as the supervisor actually runs it is covered in
:mod:`test_cache_recovery`.
"""

from pathlib import Path

import pytest
from cache_recovery_harness import machine_for, scripted_machine
from cache_recovery_helpers import DrainReading, TriggerCase, gauges

from buildlimits import cache_recovery, recovery_starvation
from buildlimits.admission import Counts
from buildlimits.systemd import SystemdError


@pytest.mark.parametrize(
    "case",
    [
        TriggerCase("starved", "live_work", gauges(queued=1, server_drain=1), True),
        TriggerCase("manual", "live_work", gauges(queued=1, server_drain=1), False),
        TriggerCase("starved", "manager", gauges(queued=1, server_drain=1), False),
        TriggerCase(
            "starved",
            "stopped_populated",
            gauges(queued=1, server_drain=1),
            False,
        ),
        TriggerCase(
            "starved",
            "live_work",
            gauges(queued=1, running=1, server_drain=1),
            False,
        ),
        TriggerCase(
            "starved",
            "live_work",
            gauges(queued=1, draining=1, server_drain=1),
            False,
        ),
        TriggerCase(
            "starved",
            "live_work",
            Counts(1, 2, 1, 0, 1, capacity=2, cached_moving=0),
            True,
        ),
        TriggerCase("starved", "live_work", gauges(server_drain=1), False),
        TriggerCase("starved", "live_work", gauges(queued=1), False),
        TriggerCase("starved", None, gauges(queued=1, server_drain=1), False),
        TriggerCase(
            "starved",
            "live_work",
            gauges(queued=1, server_drain=1),
            False,
            roots=2,
        ),
        TriggerCase(
            "starved",
            "live_work",
            gauges(queued=1, server_drain=1),
            False,
            roots=None,
        ),
    ],
    ids=[
        "starving",
        "manual-policy",
        "manager-fault",
        "stopped-populated",
        "running-group",
        "draining-group",
        "uncached-running-group",
        "empty-queue",
        "nothing-held",
        "no-fault",
        "positive-roots",
        "unknown-roots",
    ],
)
def test_only_starvation_by_live_cache_work_starts_recovery(
    tmp_path: Path, case: TriggerCase
) -> None:
    """The automatic trigger needs starvation, a live cache and a policy."""
    machine, state = machine_for(
        tmp_path, mode=case.mode, snapshot=case.counts, reading=case.reading
    )
    machine.tick(0.0)
    busy = machine.describe(0.0)["phase"] != "idle"
    assert busy is case.starts, "the trigger policy decides"

    # The transaction advances one step per tick, so the barrier and the stop
    # each need their own poll before the manager sees any control request.
    machine.tick(1.0)
    machine.tick(2.0)
    assert bool(state.verbs) is case.starts, "only a trigger asks the cache to stop"

    # However long the transaction runs, a live cache is never accounted for,
    # and a stopped cache is still not accounting while a token waits on it.
    # The ticks stay inside the staleness limit, so a case that must not start
    # is not opened by its retained drain's age instead; that path has its own
    # tests in ``test_recovery_stale_drain``.
    state.active = False
    for second in range(1, 3):
        machine.tick(float(second) * cache_recovery.RECOVERY_MAX_RETRY_SECONDS)
    phase = machine.describe(1_000.0)["phase"]
    assert (phase != "idle") is case.starts, "no elapsed time completes the transaction"
    if case.starts:
        assert phase == "accounting", "the transaction waits on token accounting"


def test_accounting_waits_for_every_server_drained_group(tmp_path: Path) -> None:
    """A transaction cannot restart the cache while any token waits on it."""
    cache = scripted_machine(tmp_path, [gauges(queued=1, server_drain=2)])
    cache.machine.request("manual", 0.0)
    cache.machine.tick(0.0)
    cache.machine.tick(1.0)
    cache.snapshot[0] = gauges(queued=1, server_drain=1)
    cache.machine.tick(2.0)
    assert cache.machine.describe(2.0)["phase"] == "accounting", (
        "one released token cannot finish the account"
    )
    cache.snapshot[0] = gauges(queued=0)
    # One tick to observe the accounting proof, one to issue the restart.
    cache.machine.tick(3.0)
    cache.machine.tick(4.0)
    assert "start" in cache.cache.verbs, "the cache restarts once nothing waits on it"


def test_a_clean_recovery_rests_only_briefly(tmp_path: Path) -> None:
    """A transaction that restarted the cache rests for the settle, not 15 min.

    Detached work can be retained again moments after a clean recovery. The
    long cooldown exists to outlast a refused restart's retries; applied to a
    clean ending it kept fresh retained tokens idle while the queue waited.
    """
    cache = scripted_machine(tmp_path, [gauges(queued=1, server_drain=1)])
    cache.machine.request("manual", 0.0)
    cache.machine.tick(0.0)
    cache.machine.tick(1.0)
    cache.snapshot[0] = gauges(queued=1)
    # One tick to observe the accounting proof, one to issue the restart.
    cache.machine.tick(2.0)
    cache.machine.tick(3.0)
    document = cache.machine.describe(3.0)
    assert document["phase"] == "idle", "the transaction has ended"
    assert not document["last_fault"], "the restart was accepted"
    assert document["cooling_for"] == pytest.approx(60.0), (
        "a clean ending rests only for the settle period"
    )


def test_a_draining_group_holds_the_stop_with_its_own_reason(tmp_path: Path) -> None:
    """A draining group is named as still moving, not as an accounting defect.

    A draining group's leader has ended, but its processes may still run, so
    stopping the cache can kill them. The request still opens, because opening
    only holds new compiles off the cache; the stop waits, and the reason it
    reports must say why, rather than describing the group as a token that
    simply has no accounting to await.
    """
    cache = scripted_machine(tmp_path, [gauges(queued=1, draining=1, server_drain=1)])
    document = cache.machine.request("manual", 0.0)
    assert not document["blocked"], "a draining group does not refuse the request"
    cache.machine.tick(0.0)
    cache.machine.tick(1.0)

    assert "moving" in str(cache.machine.describe(1.0)["last_fault"]), (
        "the waiting stop names the draining group as live work"
    )
    assert "stop" not in cache.cache.verbs, "no stop is issued under it"


def test_only_cached_groups_hold_the_stop(tmp_path: Path) -> None:
    """An uncached running group lets the stop through; a cached one does not.

    Stopping the managed cache can only interrupt a group composed with it. A
    transaction composes every group it admits without the cache, so counting
    those as moving would let a busy queue hold the stop off indefinitely.
    """
    uncached = Counts(1, 2, 1, 0, 1, capacity=2, cached_moving=0)
    cache = scripted_machine(tmp_path, [uncached])
    document = cache.machine.request("manual", 0.0)
    assert not document["blocked"], "an uncached running group blocks nothing"
    cache.machine.tick(0.0)
    cache.machine.tick(1.0)
    assert cache.cache.verbs == ["stop"], "the stop is issued past it"

    cached = scripted_machine(
        tmp_path / "cached", [Counts(1, 2, 1, 0, 1, capacity=2, cached_moving=1)]
    )
    opened = cached.machine.request("manual", 0.0)
    assert not opened["blocked"], "a cached running group does not refuse the request"
    cached.machine.tick(0.0)
    cached.machine.tick(1.0)
    assert "1 cached compiler group(s) are still moving" in str(
        cached.machine.describe(1.0)["last_fault"]
    ), "a cached running group still holds the stop"
    assert "stop" not in cached.cache.verbs, "no stop is issued under it"


def test_a_lapsed_root_proof_holds_the_automatic_stop(tmp_path: Path) -> None:
    """An automatic stop is never issued on a proof that has gone stale.

    The zero-root count is a diagnostic sample taken when the fault was
    recorded, not a state the transaction holds, so a compiler appearing in
    that window withdraws the corroboration the trigger acted on. The
    transaction then waits in the stopping phase rather than stopping a cache
    that now has visible work.
    """
    cache = scripted_machine(tmp_path, [gauges(queued=1, server_drain=1)])
    cache.machine.tick(0.0)
    cache.machine.tick(1.0)
    assert cache.cache.marker.exists(), "the automatic trigger opened a transaction"

    # A compiler root appears before the stop is due.
    cache.roots[0] = 2
    cache.machine.tick(2.0)

    assert "stop" not in cache.cache.verbs, (
        "a stop must not follow a root proof that has lapsed"
    )
    assert cache.machine.describe(2.0)["last_fault"], (
        "the lapsed proof is reported rather than silently retried"
    )
    assert cache.cache.marker.exists(), "the barrier still holds the cache down"


def test_a_manual_request_stops_even_after_the_root_proof_lapses(
    tmp_path: Path,
) -> None:
    """The operator's own request is not gated on the diagnostic sample.

    A detached-work fault is remediated by stopping the cache, and the manual
    policy is the only route for a host that must not act on its own. If the
    root count went missing — which is exactly a fault the diagnostic cannot
    read — stalling the stop would withhold that host's only remediation.
    """
    cache = scripted_machine(tmp_path, [gauges(queued=1, server_drain=1)])
    cache.machine.request("manual", 0.0)
    cache.machine.tick(0.0)
    cache.roots[0] = None
    cache.machine.tick(1.0)

    assert "stop" in cache.cache.verbs, "the operator's stop is not gated on roots"


def test_a_group_admitted_during_quiescing_keeps_the_cache_running(
    tmp_path: Path,
) -> None:
    """The stop is refused while a compiler group is admitted after the request.

    Admission stays live between the moment a transaction is requested and the
    moment its stop is issued, so a request that was already waiting can be
    granted a token in that window. Stopping the cache would kill that
    compile, which the users' guide promises never happens.
    """
    cache = scripted_machine(tmp_path, [gauges(queued=1, server_drain=1)])
    cache.machine.request("manual", 0.0)
    # Quiescing writes the barrier; the stop itself is due on the next tick.
    cache.machine.tick(0.0)
    assert cache.cache.marker.exists(), "the transaction reached its stop phase"

    # A queued request is admitted while the transaction quiesces.
    cache.snapshot[0] = gauges(queued=0, running=1, server_drain=1)
    cache.machine.tick(1.0)

    assert "stop" not in cache.cache.verbs, (
        "an admitted compiler group must not be interrupted by the stop"
    )
    assert cache.machine.describe(1.0)["last_fault"], (
        "the refusal is reported rather than silently retried"
    )
    assert cache.cache.marker.exists(), "the barrier still holds the cache down"


def test_the_recovery_limits_leave_room_for_the_restart_to_be_attempted() -> None:
    """Bound the constants themselves, so a long backoff cannot hide a retry.

    The transaction attempts its restart immediately after the accounting
    proof, so the cooldown is the only delay between that proof and the
    attempt. The whole bounded sequence of restart failures must therefore
    fit inside it; otherwise a cache that was merely slow to come up would
    exhaust the attempts at the same moment recovery became available again.
    Each attempt still outlasts the first retry delay, so the backoff has room
    to grow past its first step.
    """
    assert recovery_starvation.RECOVERY_COOLDOWN_SECONDS > (
        cache_recovery.RECOVERY_MAX_RETRY_SECONDS
    ), "the bounds are ordered, so a bounded backoff cannot outlast its cooldown"
    sequence = [
        min(
            cache_recovery.RECOVERY_RETRY_SECONDS * attempt,
            cache_recovery.RECOVERY_MAX_RETRY_SECONDS,
        )
        for attempt in range(1, cache_recovery.RECOVERY_FAILURE_LIMIT + 1)
    ]
    assert sum(sequence) < recovery_starvation.RECOVERY_COOLDOWN_SECONDS, (
        "the bounded retries finish inside one cooldown"
    )
    assert sequence[-1] > sequence[0], "the backoff has room to grow"


def test_a_failed_restart_reports_guidance_without_latching_recovery(
    tmp_path: Path,
) -> None:
    """A cache that cannot restart is reported without withholding freed tokens."""
    machine, state = machine_for(
        tmp_path, snapshot=gauges(queued=0), reading=DrainReading()
    )
    state.refuse = "start"
    machine.request("manual", 0.0)
    for second in range(4):
        machine.tick(float(second))
    document = machine.describe(3.0)
    assert "start" in state.verbs, "the restart was attempted"
    assert document["last_fault"], "the failed restart is kept for the operator"
    ended = None
    for step in range(cache_recovery.RECOVERY_FAILURE_LIMIT + 2):
        now = 1_000.0 + float(step) * cache_recovery.RECOVERY_MAX_RETRY_SECONDS
        machine.tick(now)
        if machine.describe(now)["phase"] == "idle":
            ended = now
            break
    assert ended is not None, "the refused restart exhausts its attempts"
    assert machine.describe(ended)["cooling_for"] == pytest.approx(900.0), (
        "a faulted ending rests for the whole long cooldown"
    )
    assert machine.describe(9_999.0)["phase"] == "idle", (
        "a stalled restart cannot latch an accounting that already proved stopped"
    )
    assert not state.marker.exists(), "the barrier is not restored after accounting"


def test_a_manager_fault_cannot_be_answered_by_stopping_the_cache(
    tmp_path: Path,
) -> None:
    """An unreachable manager never turns into an attempted stop."""
    machine, state = machine_for(
        tmp_path,
        snapshot=gauges(queued=1, server_drain=1),
        reading=DrainReading(kind="manager"),
    )
    machine.tick(0.0)
    assert machine.describe(0.0)["phase"] == "idle", (
        "the manager fault blocks the trigger"
    )
    assert not state.marker.exists(), "no barrier is written for a transport fault"
    with pytest.raises(SystemdError):
        machine.request_stop()
    assert machine.describe(0.0)["phase"] == "idle", (
        "a raised stop leaves the machine idle"
    )
