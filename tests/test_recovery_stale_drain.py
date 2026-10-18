"""Recovery of a retained drain on a host too busy to prove it detached.

The prompt trigger needs a moment with no cached group moving and no compiler
root visible. A busy queue refills every free slot with a cached compile at
once, so that moment need not come, and a single retained token then costs the
host a slot until every slot is retained. These tests drive the machine through
that case: the drain's age alone opens the transaction, the transaction holds
new compiles off the cache, and the stop still waits for the cached compiles
already running and for the zero-root proof.

The 300-second limit is written as a literal so a change to the production
constant is a disagreement rather than a tidy update.
"""

import typing as typ

from cache_recovery_harness import scripted_machine, tick_phase

from buildlimits.admission_state import Counts
from buildlimits.recovery_starvation import StarvationReading, starvation

if typ.TYPE_CHECKING:
    from pathlib import Path


STALE_LIMIT_SECONDS = 300.0


def _busy() -> Counts:
    """Name a busy host: one retained token and three cached compiles running.

    Returns
    -------
    Counts
        The snapshot in which the prompt proof never holds.
    """
    return Counts(9, 4, 3, 0, 1, capacity=4, cached_moving=3)


def _reading(*, roots: int, cached_moving: int, drain_age: float) -> StarvationReading:
    """Build an automatic-policy reading of one retained drain with a waiter.

    Parameters
    ----------
    roots : int
        Visible compiler roots the live-work probe counted.
    cached_moving : int
        Cached groups still running or draining.
    drain_age : float
        Seconds the drain has been held without a break.

    Returns
    -------
    StarvationReading
        A reading that every clause but the evidence clause permits.
    """
    return StarvationReading(
        cache_mode="sccache",
        policy="starved",
        cooling=0.0,
        now=1_000.0,
        counts=Counts(1, 1 + cached_moving, cached_moving, 0, 1, 4, cached_moving),
        fault_kind="live_work",
        zero_visible_roots=roots == 0,
        drain_age=drain_age,
    )


def test_each_proof_decides_the_trigger_on_its_own() -> None:
    """Each half of the evidence clause flips the trigger by itself."""
    young = STALE_LIMIT_SECONDS - 1.0
    assert starvation(_reading(roots=0, cached_moving=0, drain_age=young)), (
        "the prompt proof triggers a young drain"
    )
    assert starvation(_reading(roots=2, cached_moving=0, drain_age=young)) is None, (
        "a visible root defeats the prompt proof"
    )
    assert starvation(_reading(roots=0, cached_moving=1, drain_age=young)) is None, (
        "a moving cached group defeats the prompt proof"
    )
    stale = _reading(roots=2, cached_moving=3, drain_age=STALE_LIMIT_SECONDS)
    reason = starvation(stale)
    assert reason is not None, "age alone triggers a busy host's drain"
    assert reason.startswith("a cache drain has been retained for 300s"), reason


def test_a_stale_drain_opens_recovery_despite_cached_work(tmp_path: Path) -> None:
    """A drain retained for 300 seconds opens a transaction on a busy host."""
    cache = scripted_machine(tmp_path, [_busy()], roots=2)
    assert tick_phase(cache, 0.0) == "idle", "a fresh drain on a busy host waits"
    assert tick_phase(cache, STALE_LIMIT_SECONDS - 1.0) == "idle", (
        "a drain one second short of the limit still waits"
    )
    assert tick_phase(cache, STALE_LIMIT_SECONDS) == "quiescing", (
        "the drain's age alone opens the transaction"
    )


def test_the_stale_transaction_stops_only_once_cached_work_ends(
    tmp_path: Path,
) -> None:
    """Opening on age never stops the cache under a cached compile."""
    cache = scripted_machine(tmp_path, [_busy()], roots=2)
    tick_phase(cache, 0.0)
    tick_phase(cache, STALE_LIMIT_SECONDS)
    for second in range(1, 4):
        tick_phase(cache, STALE_LIMIT_SECONDS + float(second))
    assert "stop" not in cache.cache.verbs, "the running cached compiles are spared"
    assert cache.cache.marker.exists(), "the barrier holds the cache meanwhile"

    # The hold composes every newly admitted compile without the cache, so the
    # cached ones finish and the drain probe then finds no visible root.
    cache.snapshot[0] = Counts(9, 4, 3, 0, 1, capacity=4, cached_moving=0)
    cache.roots[0] = 0
    tick_phase(cache, STALE_LIMIT_SECONDS + 1_000.0)
    assert cache.cache.verbs == ["stop"], "the stop follows the cached compiles"


def test_a_drain_that_clears_starts_its_age_again(tmp_path: Path) -> None:
    """The age measures one unbroken retention, not the total over many."""
    cache = scripted_machine(tmp_path, [_busy()], roots=2)
    tick_phase(cache, 0.0)
    cache.snapshot[0] = Counts(9, 3, 3, 0, 0, capacity=4, cached_moving=3)
    tick_phase(cache, 200.0)
    cache.snapshot[0] = _busy()
    tick_phase(cache, 250.0)
    assert tick_phase(cache, 250.0 + STALE_LIMIT_SECONDS - 1.0) == "idle", (
        "the earlier retention does not count towards the new one"
    )
    assert tick_phase(cache, 250.0 + STALE_LIMIT_SECONDS) == "quiescing", (
        "the new retention opens recovery at its own limit"
    )
