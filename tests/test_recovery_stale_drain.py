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
from cache_recovery_scenarios import busy_supervisor, retained_counts

from buildlimits.admission_state import Counts
from buildlimits.recovery_starvation import StarvationReading, starvation

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from conftest import RecordingSink

    from buildlimits.metrics import Metrics


STALE_LIMIT_SECONDS = 300.0


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
    cache = scripted_machine(tmp_path, [retained_counts(cached_moving=3)], roots=2)
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
    cache = scripted_machine(tmp_path, [retained_counts(cached_moving=3)], roots=2)
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
    cache = scripted_machine(tmp_path, [retained_counts(cached_moving=3)], roots=2)
    tick_phase(cache, 0.0)
    cache.snapshot[0] = Counts(9, 3, 3, 0, 0, capacity=4, cached_moving=3)
    tick_phase(cache, 200.0)
    cache.snapshot[0] = retained_counts(cached_moving=3)
    tick_phase(cache, 250.0)
    assert tick_phase(cache, 250.0 + STALE_LIMIT_SECONDS - 1.0) == "idle", (
        "the earlier retention does not count towards the new one"
    )
    assert tick_phase(cache, 250.0 + STALE_LIMIT_SECONDS) == "quiescing", (
        "the new retention opens recovery at its own limit"
    )


def test_a_drain_cleared_during_accounting_starts_its_age_again(
    tmp_path: Path,
) -> None:
    """A recovery that ends the retention does not age the next drain.

    The accounting phase, not the idle tick, is what observes the count reach
    zero, so the age must be retired there. Otherwise a fresh drain that
    appears before the next idle tick inherits the previous retention's start
    and opens another recovery as soon as the cooldown allows.
    """
    cache = scripted_machine(tmp_path, [retained_counts(cached_moving=3)], roots=2)
    tick_phase(cache, 0.0)
    assert tick_phase(cache, STALE_LIMIT_SECONDS) == "quiescing", (
        "the stale drain opens the transaction"
    )

    # The cached work ends and the stop is verified, which releases the drain.
    cache.snapshot[0] = Counts(9, 3, 0, 0, 1, capacity=4, cached_moving=0)
    cache.roots[0] = 0
    now = STALE_LIMIT_SECONDS
    cleared = Counts(9, 3, 0, 0, 0, capacity=4, cached_moving=0)
    phase: object = "quiescing"
    for _ in range(10):
        now += 1.0
        if cache.machine.describe(now)["phase"] == "accounting":
            cache.snapshot[0] = cleared
        phase = tick_phase(cache, now)
        if phase == "idle":
            break
    # No further tick: an idle tick would observe the zero count itself and
    # hide the missing reset this test exists to catch.
    assert phase == "idle", "the recovery transaction ends"

    cache.snapshot[0] = retained_counts(cached_moving=3)
    cache.roots[0] = 2
    later = now + 1_000.0
    assert tick_phase(cache, later) == "idle", (
        "a fresh drain on a busy host starts its own clock"
    )
    assert tick_phase(cache, later + STALE_LIMIT_SECONDS - 1.0) == "idle", (
        "the fresh drain is not yet stale"
    )


def _sub(root: Path, name: str) -> Path:
    """Make a separate fixture directory so two machines never share a marker."""
    path = root / name
    path.mkdir()
    return path


def _stale_openings(sink: RecordingSink) -> int:
    """Sum the stale-opening counts the machine recorded."""
    key = "BUILD_LIMITS_RECOVERY_STALE_OPENINGS"
    return sum(int(record[key]) for record in sink.records if key in record)


def test_only_an_age_opened_transaction_counts_a_stale_opening(
    tmp_path: Path, metrics: Metrics, metric_sink: RecordingSink
) -> None:
    """The stale proof is told apart from the prompt proof in telemetry."""
    prompt = scripted_machine(
        _sub(tmp_path, "prompt"), [retained_counts(cached_moving=3)], roots=0
    )
    prompt.machine.metrics = metrics
    prompt.snapshot[0] = Counts(9, 3, 0, 0, 1, capacity=4, cached_moving=0)
    assert tick_phase(prompt, 0.0) == "quiescing", "the prompt proof opens at once"
    assert _stale_openings(metric_sink) == 0, "a prompt opening is not a stale one"

    stale = scripted_machine(
        _sub(tmp_path, "stale"), [retained_counts(cached_moving=3)], roots=2
    )
    stale.machine.metrics = metrics
    tick_phase(stale, 0.0)
    assert tick_phase(stale, STALE_LIMIT_SECONDS) == "quiescing", "age opens it"
    assert _stale_openings(metric_sink) == 1, "the age-opened transaction is counted"


def test_the_opening_is_journalled_with_the_retained_age(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator can read why the automatic trigger acted from the journal."""
    cache = scripted_machine(tmp_path, [retained_counts(cached_moving=3)], roots=2)
    tick_phase(cache, 0.0)
    tick_phase(cache, STALE_LIMIT_SECONDS)
    assert "a cache drain has been retained for 300s" in capsys.readouterr().err, (
        "the journal line carries the reason, with the age"
    )


def test_the_supervisor_opens_the_automatic_transaction_on_a_stale_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production supervisor wiring acts on age alone under ``starved``.

    A cached group keeps running and a visible root defeats the prompt proof,
    so only the retained age can open the transaction. The supervisor's own
    polling order, admission counts and server watch supply every reading.
    """
    harness = busy_supervisor(tmp_path, monkeypatch)

    harness.tick()
    assert harness.described()["phase"] == "idle", "a fresh drain waits"
    harness.advance(STALE_LIMIT_SECONDS - 1.0)
    harness.tick()
    assert harness.described()["phase"] == "idle", "one second short still waits"
    harness.advance(1.0)
    harness.tick()
    assert harness.described()["phase"] == "quiescing", (
        "the supervisor opens the transaction once the drain is stale"
    )
