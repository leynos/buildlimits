"""When the automatic trigger will open, as the shared query reports it.

``opens_in`` answers, from the very clauses ``starvation`` judges, how long the
trigger still has to wait on time alone. Status prints that answer, so it must
never promise an opening the trigger will not perform: the properties below
tie the two together, and the examples pin the 299 and 300 second boundaries
with literals so a change to the production constant is a disagreement rather
than a tidy update.
"""

import dataclasses as dc

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from buildlimits import recovery_outlook
from buildlimits.admission_state import Counts
from buildlimits.recovery_starvation import StarvationReading, starvation

NOW = 1_000.0


def _reading(
    *,
    drain_age: float,
    roots: int = 2,
    cached_moving: int = 3,
    queued: int = 1,
    server_drain: int = 1,
    fault_kind: str | None = "live_work",
    policy: str = "starved",
    cache_mode: str = "sccache",
    cooling: float = 0.0,
) -> StarvationReading:
    """Build a reading of one retained drain; by default only age is missing.

    Returns
    -------
    StarvationReading
        A busy-host reading with a visible root, so the prompt proof fails.
    """
    return StarvationReading(
        cache_mode=cache_mode,
        policy=policy,
        cooling=cooling,
        now=NOW,
        counts=Counts(queued, 4, cached_moving, 0, server_drain, 4, cached_moving),
        fault_kind=fault_kind,
        zero_visible_roots=roots == 0,
        drain_age=drain_age,
    )


def _later(reading: StarvationReading, seconds: float) -> StarvationReading:
    """Let time pass with every other clause unchanged.

    Returns
    -------
    StarvationReading
        The reading, with the clock and the drain's age advanced together.
    """
    return dc.replace(
        reading, now=reading.now + seconds, drain_age=reading.drain_age + seconds
    )


@pytest.mark.parametrize(
    ("drain_age", "expected"),
    [(0.0, 300.0), (120.5, 179.5), (299.0, 1.0), (300.0, 0.0), (450.0, 0.0)],
)
def test_the_countdown_runs_to_the_three_hundred_second_limit(
    drain_age: float, expected: float
) -> None:
    """The stale proof holds at 300 seconds, so 299 seconds still has 1 to go."""
    assert recovery_outlook.opens_in(_reading(drain_age=drain_age)) == expected, (
        "the wait is the 300 second limit less the drain's age"
    )


def test_the_boundary_agrees_with_the_trigger() -> None:
    """One second short the trigger waits; at the limit it acts."""
    assert starvation(_reading(drain_age=299.0)) is None, "299 seconds is too young"
    assert starvation(_reading(drain_age=300.0)) is not None, "300 seconds acts"


def test_a_provably_detached_drain_has_nothing_to_wait_for() -> None:
    """The prompt proof opens at once, however young the drain."""
    reading = _reading(drain_age=1.0, roots=0, cached_moving=0)
    assert recovery_outlook.opens_in(reading) == pytest.approx(0.0), (
        "the trigger acts now"
    )
    assert starvation(reading) is not None, "and the trigger agrees"


@pytest.mark.parametrize(
    "clause",
    [
        {"cache_mode": "direct"},
        {"policy": "manual"},
        {"fault_kind": None},
        {"fault_kind": "stopped_populated"},
        {"fault_kind": "manager"},
        {"server_drain": 0},
        {"queued": 0},
    ],
    ids=lambda clause: next(iter(clause.values())) or "no-fault",
)
def test_a_failed_clause_shows_no_countdown(clause: dict[str, object]) -> None:
    """Time cannot open a trigger whose other clauses refuse it."""
    reading = _reading(drain_age=10.0, **clause)  # type: ignore[arg-type]
    assert recovery_outlook.opens_in(reading) is None, (
        "no countdown is promised for a recovery that will not come"
    )


def test_the_cooldown_delays_the_countdown() -> None:
    """A resting trigger opens at the later of its cooldown and the limit."""
    resting = _reading(drain_age=250.0, cooling=NOW + 80.0)
    assert recovery_outlook.opens_in(resting) == pytest.approx(80.0), (
        "the cooldown is later"
    )
    young = _reading(drain_age=100.0, cooling=NOW + 80.0)
    assert recovery_outlook.opens_in(young) == pytest.approx(200.0), (
        "the limit is later"
    )
    assert starvation(_later(resting, 79.0)) is None, "still cooling at 79 seconds"
    assert starvation(_later(resting, 80.0)) is not None, "acts as it cools"


def test_a_cooldown_alone_counts_down_for_a_stale_drain() -> None:
    """A drain already stale waits only for the cooldown."""
    reading = _reading(drain_age=900.0, cooling=NOW + 42.5)
    assert recovery_outlook.opens_in(reading) == pytest.approx(42.5), (
        "only cooling remains"
    )


def _grid(upper: int) -> st.SearchStrategy[float]:
    """Draw half-second multiples, so float arithmetic here is exact.

    Returns
    -------
    st.SearchStrategy[float]
        Values from zero to ``upper`` half-seconds.
    """
    return st.integers(0, upper).map(lambda halves: halves / 2)


@st.composite
def readings(draw: st.DrawFn) -> StarvationReading:
    """Draw readings across every clause, biased to reach a positive countdown.

    Returns
    -------
    StarvationReading
        An arbitrary reading with an exact-arithmetic clock.
    """
    return _reading(
        drain_age=draw(_grid(800)),
        roots=draw(st.sampled_from([0, 2])),
        cached_moving=draw(st.integers(0, 3)),
        queued=draw(st.integers(0, 2)),
        server_drain=draw(st.integers(0, 2)),
        fault_kind=draw(
            st.sampled_from(["live_work"] * 4 + ["manager", "stopped_populated", None])
        ),
        policy=draw(st.sampled_from(["starved"] * 4 + ["manual"])),
        cache_mode=draw(st.sampled_from(["sccache"] * 4 + ["direct"])),
        cooling=NOW + draw(_grid(400)) - 100.0,
    )


@given(readings())
def test_a_shown_countdown_ends_with_the_trigger_opening(
    reading: StarvationReading,
) -> None:
    """If a countdown is shown, the trigger opens when it runs out, not before."""
    wait = recovery_outlook.opens_in(reading)
    event(f"countdown {'shown' if wait is not None else 'absent'}")
    if wait is None:
        return
    event("countdown positive" if wait > 0 else "countdown zero")
    assert starvation(_later(reading, wait)) is not None, (
        "the trigger opens once the countdown has run out"
    )
    if wait > 0:
        assert starvation(_later(reading, wait - 0.5)) is None, (
            "and not a half-second sooner, so the countdown promises no more"
        )


@given(readings(), _grid(2_000))
def test_no_countdown_means_time_alone_never_opens_the_trigger(
    reading: StarvationReading, passed: float
) -> None:
    """If the trigger can never open, no countdown is shown."""
    if recovery_outlook.opens_in(reading) is not None:
        return
    assert starvation(_later(reading, passed)) is None, (
        "a reading with no countdown cannot be opened by waiting"
    )


@given(readings())
def test_a_zero_countdown_is_exactly_an_opening_trigger(
    reading: StarvationReading,
) -> None:
    """The query and the trigger share their clauses, so they agree on now."""
    is_open = starvation(reading) is not None
    wait = recovery_outlook.opens_in(reading)
    assert (wait is not None and wait <= 0.0) is is_open, (
        "the trigger acts exactly when nothing is left to wait for"
    )
