"""How long the automatic recovery trigger rests after a transaction ends.

The expected durations here are literals, not the production constants, so a
change to a constant and to its use together still fails a test: 60 seconds
after a clean recovery, and 900 seconds after one whose restart stayed refused.
Each rest is checked at both edges of its boundary through the real trigger.
"""

import typing as typ

from cache_recovery_harness import scripted_machine
from cache_recovery_helpers import gauges

from buildlimits.recovery_starvation import cooldown_after

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cache_recovery_harness import ScriptedCache

    from buildlimits.admission_state import Counts

CLEAN_REST_SECONDS = 60.0
FAULTED_REST_SECONDS = 900.0


def _starving() -> Counts:
    """Name the gauges that starve admission: one retained token, one waiter.

    Returns
    -------
    Counts
        The snapshot the automatic trigger acts on.
    """
    return gauges(queued=1, server_drain=1)


def _run_to_idle(cache: ScriptedCache, start: float) -> float:
    """Drive an automatic transaction to its end, one second per tick.

    The accounting proof is supplied once the stop has been issued, so the
    transaction ends either with an accepted restart or with its restart
    attempts exhausted.

    Parameters
    ----------
    cache : ScriptedCache
        The machine, already holding the starving gauges.
    start : float
        The instant of the tick that opens the transaction.

    Returns
    -------
    float
        The instant of the tick that ended the transaction.
    """
    cache.machine.tick(start)
    assert cache.machine.describe(start)["phase"] == "quiescing", (
        "the starving reading opens a transaction"
    )
    now = start
    while cache.machine.describe(now)["phase"] != "idle":
        now += 1.0
        if "stop" in cache.cache.verbs:
            cache.snapshot[0] = gauges(queued=1)
        cache.machine.tick(now)
        assert now < start + 2_000.0, "the transaction ends within its bounds"
    return now


def _assert_rest(cache: ScriptedCache, ended: float, rest: float) -> None:
    """Check the reported rest and the trigger on both sides of its boundary.

    Parameters
    ----------
    cache : ScriptedCache
        The machine whose transaction has just ended.
    ended : float
        The instant the transaction ended.
    rest : float
        The literal rest the ending must leave.
    """
    assert cache.machine.describe(ended)["cooling_for"] == rest, (
        "the document reports the whole rest at the ending"
    )
    cache.snapshot[0] = _starving()
    cache.machine.tick(ended + rest - 0.5)
    assert cache.machine.describe(ended + rest - 0.5)["phase"] == "idle", (
        "the trigger stays idle inside the rest"
    )
    cache.machine.tick(ended + rest)
    assert cache.machine.describe(ended + rest)["phase"] == "quiescing", (
        "the trigger acts again as soon as the rest ends"
    )


def test_the_rests_have_their_documented_lengths() -> None:
    """A clean ending rests 60 seconds; a refused restart rests 900."""
    assert cooldown_after(faulted=False) == CLEAN_REST_SECONDS, "clean rest"
    assert cooldown_after(faulted=True) == FAULTED_REST_SECONDS, "faulted rest"


def test_a_clean_recovery_lets_the_trigger_act_after_sixty_seconds(
    tmp_path: Path,
) -> None:
    """A retained drain soon after a clean recovery is recovered within a minute."""
    cache = scripted_machine(tmp_path, [_starving()])
    ended = _run_to_idle(cache, 0.0)
    assert cache.cache.verbs == ["stop", "start"], "the restart was accepted"
    _assert_rest(cache, ended, CLEAN_REST_SECONDS)


def test_a_refused_restart_holds_the_trigger_for_fifteen_minutes(
    tmp_path: Path,
) -> None:
    """A cache that would not restart is not stopped again for 900 seconds."""
    cache = scripted_machine(tmp_path, [_starving()])
    cache.cache.refuse = "start"
    ended = _run_to_idle(cache, 0.0)
    assert cache.machine.describe(ended)["last_fault"], "the ending was faulted"
    _assert_rest(cache, ended, FAULTED_REST_SECONDS)
