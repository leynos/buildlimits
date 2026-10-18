"""Recovery of shared admission after cached work outlives its requester.

The machine under test writes a restart barrier, asks the manager to stop the
managed cache, waits for the accounting proof that the existing drain probe
produces, and then restarts the cache. Failures before the proof must leave
both the tokens and the barrier exactly where they were: no elapsed time, no
idle CPU and no missing compiler process may stand in for that proof.
"""

import typing as typ
from pathlib import Path

from cache_recovery_barrier_helpers import unremovable_barrier
from cache_recovery_harness import (
    harness_for,
    hold_drain_beside_running_group,
    hold_drained_group,
    opened_recovery,
    queue_request,
)

from buildlimits import cache_recovery, recovery_starvation

if typ.TYPE_CHECKING:
    import pytest


def test_the_transaction_stops_accounts_and_restarts_the_managed_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One transaction proves accounting before any token can return."""
    harness = opened_recovery(tmp_path, monkeypatch, queued=(2,))
    supervisor, pool = harness.supervisor, harness.pool
    assert harness.described()["phase"] == "quiescing", (
        "a manual request opens the transaction"
    )
    assert not harness.marker.exists(), "the barrier waits for the first tick"

    harness.tick()
    assert harness.marker.exists(), "the barrier is written before the stop request"
    assert not harness.cache.verbs, "a barrier is written before the cache is asked"
    assert harness.described()["phase"] == "stopping", (
        "the barrier settles into the stopping phase"
    )

    harness.tick()
    assert harness.cache.marker_at_stop is True, "the barrier precedes the stop request"
    assert harness.cache.verbs == ["stop"], "the cache was asked to stop once"

    harness.tick()
    assert harness.described()["phase"] == "accounting", (
        "a live cache is never accounted for"
    )
    assert not pool.returned, "a live cache releases nothing"
    assert supervisor.admission.tokens_held() == 1, "the drained token stays held"

    # The cache stops late: only the verified stopped-and-empty snapshot releases.
    harness.cache.active = False
    harness.advance(31)
    harness.tick()
    assert pool.returned == [b"+"], "the accounted token returns exactly once"
    assert supervisor.admission.tokens_held() == 0, "its group is gone"
    assert harness.marker.exists(), "the barrier lives until the cache restarts"
    assert harness.described()["phase"] == "restarting", (
        "the restart follows a verified accounting"
    )
    assert harness.cache.verbs == ["stop"], "no restart before accounting"

    harness.tick()
    assert harness.cache.marker_at_start is False, (
        "the barrier is gone before the start"
    )
    assert harness.cache.verbs == ["stop", "start"], "the cache was asked to start once"
    assert harness.described()["phase"] == "idle", (
        "the completed transaction leaves no recovery state"
    )

    harness.tick()
    assert harness.cache.verbs == ["stop", "start"], (
        "a finished transaction does nothing"
    )

    harness.advance(recovery_starvation.RECOVERY_COOLDOWN_SECONDS)
    harness.tick()
    assert harness.described()["phase"] == "idle", (
        "a healed admission does not re-enter"
    )


def test_duplicate_requests_join_the_running_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeated request reports the transaction instead of restarting it."""
    harness = harness_for(tmp_path, monkeypatch)
    first = harness.supervisor.recovery.request("manual", harness.now)
    harness.advance(4)
    second = harness.supervisor.recovery.request("manual", harness.now)
    assert second == first, "the second request joins the first transaction"


def test_failed_stops_keep_tokens_and_the_barrier_with_bounded_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused stop retries with growing, bounded delays and keeps ownership."""
    harness = opened_recovery(tmp_path, monkeypatch, refuse="stop")
    supervisor = harness.supervisor
    harness.tick()

    delays: list[float] = []
    for _ in range(cache_recovery.RECOVERY_FAILURE_LIMIT + 2):
        harness.tick()
        document = harness.described()
        attempts = typ.cast("int", document["attempts"])
        assert attempts >= 1, "the refused stop is counted in every step"
        assert document["last_fault"], "the refusal is kept for the operator"
        assert harness.cache.last_attempted("stop"), "the stop was attempted again"
        delays.append(typ.cast("float", document["retry_in"]))
        assert supervisor.admission.tokens_held() == 1, "a refused stop keeps the token"
        assert harness.marker.exists(), "a refused stop keeps the barrier"
        assert document["phase"] == "stopping", (
            "a refused phase is retried, not skipped"
        )
        harness.advance(typ.cast("float", document["retry_in"]) or 1.0)

    assert delays == sorted(delays), "the retry delay never shrinks"
    assert delays[-1] > delays[0], "the retry delay grows past the first attempt"
    assert max(delays) == cache_recovery.RECOVERY_MAX_RETRY_SECONDS, (
        "the limit is reachable, so the ceiling is enforced rather than decorative"
    )


def test_a_request_under_a_running_compiler_waits_to_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery opens beside a running cached compiler but never stops under it.

    Refusing the request outright left an operator no way in on a busy host,
    where a cached compile is almost always running. Opening only writes the
    barrier and holds new compiles off the cache; the stop waits for the
    running group to finish.
    """
    harness = harness_for(tmp_path, monkeypatch)
    supervisor = harness.supervisor
    hold_drain_beside_running_group(supervisor)

    document = supervisor.recovery.request("manual", harness.now)
    assert not document["blocked"], "a running cached group does not refuse it"
    harness.tick()
    assert harness.marker.exists(), "the barrier is written beside the compile"
    harness.tick()
    assert "stop" not in harness.cache.verbs, "the cache is not stopped under it"
    assert "still moving" in str(harness.described()["last_fault"]), (
        "the waiting stop says what it waits for"
    )
    assert supervisor.admission.tokens_held() == 2, "the running group keeps its token"

    # The compile finishes; the retried stop now finds nothing cached moving.
    del supervisor.admission.groups["g2"]
    harness.advance(cache_recovery.RECOVERY_MAX_RETRY_SECONDS)
    harness.tick()
    assert harness.cache.verbs == ["stop"], "the stop follows the compile"


def test_a_queued_request_keeps_its_progress_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery never takes a waiting request's token or its replies away."""
    harness = opened_recovery(tmp_path, monkeypatch, queued=(7,))
    supervisor, pool = harness.supervisor, harness.pool
    harness.tick()
    harness.tick()

    document = supervisor.status()
    queued = typ.cast("list[dict[str, object]]", document["queued"])
    transaction = typ.cast("dict[str, object]", document["cache_recovery"])
    assert [item["request"] for item in queued] == [7], (
        "the waiting request is still reported"
    )
    assert transaction["phase"] == "accounting", "recovery is still running"
    assert not pool.returned, "a stopped-but-live cache releases nothing"

    supervisor.admission.cancel(7)
    assert 7 not in supervisor.admission.queue, "the cancellation is honoured"
    assert supervisor.admission.tokens_held() == 1, (
        "a cancelled waiting request never held a token"
    )


def test_a_repeated_cancellation_cannot_lose_a_token_or_overflow_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second drain group mid-recovery conserves every token and the capacity."""
    harness = opened_recovery(tmp_path, monkeypatch, queued=(2,))
    supervisor, pool = harness.supervisor, harness.pool
    harness.tick()
    harness.tick()

    # A second cancelled cached group lands in server-drain while the
    # transaction is waiting, so accounting now has two tokens to return.
    harness.pool.request()
    hold_drained_group(supervisor, "g2")
    queue_request(supervisor, request_id=3)
    harness.tick()

    harness.cache.active = False
    harness.advance(40)
    harness.tick()
    assert sorted(pool.returned) == [b"+", b"+"], "each drain group returns one token"
    assert supervisor.admission.tokens_held() == 0, "no group survives accounting"
    assert pool.max_held <= pool.capacity, "capacity was never exceeded"
    assert list(supervisor.admission.queue) == [2, 3], (
        "both waiting requesters keep their place through recovery"
    )


def test_a_failed_recovery_cannot_return_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop the manager refuses keeps every token, the barrier and the fault."""
    harness = opened_recovery(tmp_path, monkeypatch, refuse="stop")
    supervisor = harness.supervisor
    harness.tick()  # quiescing: the barrier is written
    for attempt in range(cache_recovery.RECOVERY_FAILURE_LIMIT):
        harness.advance(cache_recovery.RECOVERY_MAX_RETRY_SECONDS)
        harness.tick()
        assert supervisor.admission.tokens_held() == 1, (
            f"attempt {attempt} cannot release a token by failing"
        )
        assert not harness.pool.returned, "a live cache releases nothing"
        assert harness.marker.exists(), "the barrier outlives every failed stop"
    assert harness.described()["phase"] == "stopping", (
        "a refused stop is retried rather than abandoned"
    )
    assert harness.described()["attempts"] == cache_recovery.RECOVERY_FAILURE_LIMIT, (
        "the failure limit is visible to the operator"
    )
    assert harness.described()["last_fault"], (
        "the refused stop stays visible to the operator"
    )


def test_a_restart_the_manager_refuses_still_ends_the_accounted_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accounting that succeeded is not undone by a cache that cannot restart."""
    harness = opened_recovery(tmp_path, monkeypatch, refuse="start")
    supervisor, pool = harness.supervisor, harness.pool
    harness.account()
    harness.tick()  # restarting: the start is refused
    assert pool.returned == [b"+"], "the accounted token returns exactly once"
    assert supervisor.admission.tokens_held() == 0, "its group is gone"
    assert not harness.marker.exists(), (
        "an accounted transaction leaves no barrier behind"
    )
    assert harness.described()["last_fault"] is not None, (
        "the refused start stays visible to the operator"
    )
    harness.exhaust_retries()
    assert harness.described()["phase"] == "idle", (
        "a stalled restart cannot latch an accounting that already proved stopped"
    )
    assert pool.returned == [b"+"], "the retries cannot return the token twice"


def test_a_barrier_that_cannot_be_removed_is_retried_not_reported_as_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start that the unit's own condition would skip is not a clean finish.

    The unit refuses to start while the barrier exists, so queueing a start
    whose barrier could not be removed asks for a cache that can never come
    back. Reporting that as a completed transaction would retire the fault
    into a cooldown the automatic trigger cannot cross, so the refusal must
    keep the transaction in its restart phase with the barrier intact.
    """
    harness = opened_recovery(tmp_path, monkeypatch)
    pool = harness.pool
    harness.account()
    assert pool.returned == [b"+"], "the accounted token returns once"

    unremovable_barrier(monkeypatch, harness.marker, "Read-only file system")
    harness.tick()  # restarting: the barrier cannot be cleared
    document = harness.described()
    assert document["phase"] == "restarting", (
        "a transaction that cannot free the cache keeps trying"
    )
    assert document["attempts"] == 1, "the refused removal is counted as a failure"
    assert "cannot clear" in str(document["last_fault"]), (
        "the removal refusal is kept for the operator"
    )
    assert harness.cache.verbs == ["stop"], (
        "no start is queued that the unit's own condition would skip"
    )
    assert harness.marker.exists(), "the barrier is still there to be retried"

    harness.advance(typ.cast("float", document["retry_in"]) or 1.0)
    harness.tick()
    assert harness.described()["attempts"] == 2, "the removal is attempted again"
    assert harness.cache.verbs == ["stop"], "the start is still withheld"
    assert pool.returned == [b"+"], "retrying cannot return the token twice"

    harness.exhaust_retries()
    assert harness.described()["phase"] == "idle", (
        "a barrier that cannot be removed cannot latch the transaction for ever"
    )
    assert harness.marker.exists(), (
        "the exhausted transaction leaves the barrier for the operator's own run"
    )
    assert pool.returned == [b"+"], "no retry returns a token a second time"
