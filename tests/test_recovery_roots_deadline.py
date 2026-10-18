"""The automatic stop's second deadline: roots that outlive every client.

The zero-root proof gates an automatic stop, but a compile hung inside the
managed server keeps a root visible for ever, so the retained tokens were never
recovered without an operator. Once no cached group is moving, a visible root
serves no admitted client; these tests drive the machine through that case and
pin both edges of the deadline.

The 600-second grace is written as a literal so a change to the production
constant is a disagreement rather than a tidy update.
"""

import typing as typ

import pytest
from cache_recovery_machines import scripted_machine, tick_phase
from cache_recovery_scenarios import busy_supervisor, retained_counts

from buildlimits.admission_state import Counts
from buildlimits.recovery_roots import VisibleRootsGrace, roots_verdict

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cache_recovery_machines import ScriptedCache
    from conftest import RecordingSink

    from buildlimits.metrics import Metrics

BUSY = retained_counts(cached_moving=3)
QUIET = retained_counts(cached_moving=0)
GRACE_SECONDS = 600.0
STALE_SECONDS = 300.0
# Attempts fail with a backoff of 30, 60, 90 and then 120 seconds, so after the
# first attempt at offset zero the next are due at 30, 90, 180, 300, 420, 540
# and 660 seconds: the first attempt at or after the grace is the last one.
LAST_ATTEMPT_BEFORE_GRACE = 540.0
FIRST_ATTEMPT_AT_GRACE = 660.0


def _waited(cache: ScriptedCache, now: float) -> float:
    """Read how long the status document says roots have delayed the stop.

    Returns
    -------
    float
        The reported ``roots_waited`` seconds.
    """
    return typ.cast("float", cache.machine.describe(now)["roots_waited"])


def _tick_each_second(cache: ScriptedCache, start: float, seconds: float) -> float:
    """Tick once per second so every due attempt is taken at its own instant.

    Returns
    -------
    float
        The instant of the last tick.
    """
    now = start
    for _ in range(int(seconds)):
        now += 1.0
        cache.machine.tick(now)
    return now


def _stopping_with_a_hung_root(cache: ScriptedCache) -> float:
    """Open on age, let cached work end, and reach the first blocked-by-root stop.

    Returns
    -------
    float
        The instant of that first stop attempt, where the grace clock starts.
    """
    tick_phase(cache, 0.0)
    assert tick_phase(cache, STALE_SECONDS) == "quiescing", "age opens it"
    cache.snapshot[0] = QUIET
    started = STALE_SECONDS + 1.0
    assert tick_phase(cache, started) == "stopping", "the barrier is written"
    assert tick_phase(cache, started + 1.0) == "stopping", "the first stop waits"
    assert cache.cache.verbs == [], "a visible root defeats the zero-root proof"
    return started + 1.0


def test_a_hung_root_no_longer_holds_the_stop_for_ever(tmp_path: Path) -> None:
    """Roots visible for the whole grace with nothing moving let the stop go."""
    cache = scripted_machine(tmp_path, [BUSY], roots=2)
    first = _stopping_with_a_hung_root(cache)

    _tick_each_second(cache, first, int(LAST_ATTEMPT_BEFORE_GRACE))
    assert cache.cache.verbs == [], "the grace has not yet passed"
    now = _tick_each_second(
        cache, first + LAST_ATTEMPT_BEFORE_GRACE, int(FIRST_ATTEMPT_AT_GRACE - 540)
    )
    assert cache.cache.verbs == ["stop"], "the stop proceeds on the deadline"
    assert tick_phase(cache, now + 1.0) == "accounting", "accounting follows"


def test_the_grace_is_measured_from_the_first_unblocked_attempt(
    tmp_path: Path,
) -> None:
    """The clock is reported to operators and starts when nothing is moving."""
    cache = scripted_machine(tmp_path, [BUSY], roots=2)
    first = _stopping_with_a_hung_root(cache)
    assert _waited(cache, first) == pytest.approx(0.0), "the clock starts"
    later = first + 100.0
    assert _waited(cache, later) == pytest.approx(100.0), (
        "the status document reports how long the roots have delayed the stop"
    )


def test_cached_work_moving_again_restarts_the_grace(tmp_path: Path) -> None:
    """A cached group that moves again breaks the run the grace measures."""
    cache = scripted_machine(tmp_path, [BUSY], roots=2)
    first = _stopping_with_a_hung_root(cache)
    now = _tick_each_second(cache, first, 400)

    cache.snapshot[0] = BUSY
    now = _tick_each_second(cache, now, 300)
    assert _waited(cache, now) == pytest.approx(0.0), "the run is broken"
    cache.snapshot[0] = QUIET
    resumed = _tick_each_second(cache, now, 200)
    assert cache.cache.verbs == [], "time before the break does not count"
    assert _waited(cache, resumed) > 0.0, "a new run began"
    _tick_each_second(cache, resumed, int(FIRST_ATTEMPT_AT_GRACE))
    assert cache.cache.verbs == ["stop"], "the new run reaches its own deadline"


def test_a_later_transaction_does_not_inherit_the_clock(tmp_path: Path) -> None:
    """The grace is retired with its transaction, as the drain age was."""
    cache = scripted_machine(tmp_path, [BUSY], roots=2)
    first = _stopping_with_a_hung_root(cache)
    now = _tick_each_second(cache, first, int(FIRST_ATTEMPT_AT_GRACE))
    assert cache.cache.verbs == ["stop"], "the first transaction stops on deadline"

    cache.snapshot[0] = Counts(9, 3, 0, 0, 0, capacity=4, cached_moving=0)
    now = _tick_each_second(cache, now, 5)
    assert tick_phase(cache, now + 1.0) == "idle", "the transaction ends"

    cache.snapshot[0] = BUSY
    now = _tick_each_second(cache, now + 1.0 + 1_000.0, int(STALE_SECONDS) + 1)
    cache.snapshot[0] = QUIET
    now = _tick_each_second(cache, now, int(LAST_ATTEMPT_BEFORE_GRACE))
    assert cache.cache.verbs == ["stop", "start"], "the second one still waits"
    assert _waited(cache, now) < GRACE_SECONDS, "the old start was not inherited"


def test_a_manual_request_never_starts_the_clock(tmp_path: Path) -> None:
    """The operator's own evidence is unchanged: no root check, no clock."""
    cache = scripted_machine(tmp_path, [QUIET], roots=2)
    cache.machine.request("manual", 0.0)
    cache.machine.tick(0.0)
    cache.machine.tick(1.0)
    assert cache.cache.verbs == ["stop"], "a manual stop needs no zero-root proof"
    assert _waited(cache, 1.0) == pytest.approx(0.0), "no clock ran"


def test_an_unknown_root_count_never_reaches_the_deadline(tmp_path: Path) -> None:
    """A count the diagnostic could not read is no evidence of a visible root."""
    cache = scripted_machine(tmp_path, [BUSY], roots=None)
    first = _stopping_with_a_hung_root(cache)
    _tick_each_second(cache, first, int(FIRST_ATTEMPT_AT_GRACE) + 600)
    assert cache.cache.verbs == [], "no root was ever seen, so none delayed the stop"
    assert _waited(cache, first + 1_000.0) == pytest.approx(0.0), "no clock ran"


def test_a_reading_that_loses_the_count_breaks_the_run(tmp_path: Path) -> None:
    """A run of visible roots is broken by a reading that cannot see them."""
    cache = scripted_machine(tmp_path, [BUSY], roots=2)
    first = _stopping_with_a_hung_root(cache)
    now = _tick_each_second(cache, first, 400)
    cache.roots[0] = None
    now = _tick_each_second(cache, now, 100)
    assert _waited(cache, now) == pytest.approx(0.0), "the unknown reading broke it"
    cache.roots[0] = 2
    _tick_each_second(cache, now, int(FIRST_ATTEMPT_AT_GRACE) - 100)
    assert cache.cache.verbs == [], "the earlier run does not count"


def test_a_faulted_manager_still_refuses_the_deadline_stop(tmp_path: Path) -> None:
    """The deadline never overrides the manager-fault refusal."""
    cache = scripted_machine(tmp_path, [BUSY], roots=2)
    first = _stopping_with_a_hung_root(cache)
    cache.kind[0] = "manager"
    _tick_each_second(cache, first, int(FIRST_ATTEMPT_AT_GRACE) + 600)
    assert cache.cache.verbs == [], "an unreadable cache is never stopped"


@pytest.mark.parametrize(
    ("is_automatic", "is_zero", "waited", "expected"),
    [
        (True, False, 0.0, "waiting"),
        (True, False, 599.999, "waiting"),
        (True, False, 600.0, "deadline"),
        (True, True, 0.0, "proven"),
        (False, False, 0.0, "exempt"),
    ],
)
def test_the_verdict_has_a_boundary_at_exactly_the_limit(
    *, is_automatic: bool, is_zero: bool, waited: float, expected: str
) -> None:
    """Both edges of the pure verdict, and the cases that need no grace."""
    verdict = roots_verdict(
        is_automatic=is_automatic, zero_visible_roots=is_zero, waited=waited
    )
    assert verdict == expected, f"after {waited}s"


def test_the_grace_clock_accrues_and_breaks() -> None:
    """The clock starts on the first reading and clear roots break the run."""
    grace = VisibleRootsGrace()
    readings = [(True, 5.0, 0.0), (True, 605.0, 600.0), (False, 606.0, 0.0)]
    for is_visible, now, expected in readings:
        assert grace.observe(is_visible=is_visible, now=now) == pytest.approx(
            expected
        ), f"at {now}"
    assert grace.elapsed(700.0) == pytest.approx(0.0), "a broken run has no time"


def test_the_deadline_stop_is_journalled_and_counted(
    tmp_path: Path,
    metrics: Metrics,
    metric_sink: RecordingSink,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The operator can tell a deadline stop from a proved one."""
    cache = scripted_machine(tmp_path, [BUSY], roots=2)
    cache.machine.metrics = metrics
    first = _stopping_with_a_hung_root(cache)
    key = "BUILD_LIMITS_RECOVERY_ROOTS_DEADLINE_STOPS"
    _tick_each_second(cache, first, int(FIRST_ATTEMPT_AT_GRACE - 1))
    assert not any(key in record for record in metric_sink.records), "not yet"
    _tick_each_second(cache, first + FIRST_ATTEMPT_AT_GRACE - 1, 1)
    counted = sum(int(record[key]) for record in metric_sink.records if key in record)
    assert counted == 1, "exactly one deadline stop is counted"
    assert "compiler roots still visible after 660s" in capsys.readouterr().err, (
        "the journal line carries how long the roots delayed the stop"
    )


def test_a_proved_stop_is_not_counted_as_a_deadline_stop(
    tmp_path: Path, metrics: Metrics, metric_sink: RecordingSink
) -> None:
    """Only the deadline path adds to the counter."""
    cache = scripted_machine(tmp_path, [QUIET], roots=0)
    cache.machine.metrics = metrics
    tick_phase(cache, 0.0)
    tick_phase(cache, 1.0)
    tick_phase(cache, 2.0)
    assert cache.cache.verbs == ["stop"], "the proof let the stop go"
    key = "BUILD_LIMITS_RECOVERY_ROOTS_DEADLINE_STOPS"
    assert not any(key in record for record in metric_sink.records), (
        "a proved stop adds no deadline count"
    )


def test_the_supervisor_stops_on_the_roots_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production wiring stops a stale transaction whose root never clears."""
    with busy_supervisor(tmp_path, monkeypatch) as harness:
        supervisor = harness.supervisor
        # The scripted cache runs no compiler, so every probe would read zero roots;
        # a hung compile is what keeps the proof from ever being established.
        for name, value in (("zero_visible_roots", False), ("roots_are_visible", True)):
            monkeypatch.setattr(
                type(supervisor.server), name, property(lambda _, v=value: v)
            )
        harness.tick()
        harness.advance(STALE_SECONDS)
        harness.tick()
        assert harness.described()["phase"] == "quiescing", "age opens the transaction"

        # The cached group ends; the hung root stays visible in the server.
        del supervisor.admission.groups["g2"]
        for _ in range(int(FIRST_ATTEMPT_AT_GRACE) + 60):
            harness.advance(1.0)
            harness.tick()
            if "stop" in harness.cache.verbs:
                break
        assert harness.cache.verbs == ["stop"], "the stop went on the deadline"
        assert harness.now >= STALE_SECONDS + GRACE_SECONDS, "and not before the grace"
