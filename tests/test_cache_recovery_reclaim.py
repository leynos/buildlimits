"""Start-up reclamation of a barrier an interrupted transaction left behind.

A supervisor that dies mid-transaction leaves the restart barrier in place. Its
next start-up must first prove the cache stopped and empty, and only then does
the barrier become obsolete: clearing it and restarting the cache are what
return the host to caching. The barrier is what the managed unit's
``ConditionPathExists=!`` reads, so a barrier that cannot be cleared is not a
reason to queue a start that the unit will skip — it is a fault to report.
"""

import contextlib
from pathlib import Path

import pytest
from cache_recovery_barrier_helpers import (
    BarrierFate,
    unreadable_barrier,
    unremovable_barrier,
)
from cache_recovery_helpers import (
    CACHE_UNIT,
    OWN_CGROUP,
    CacheState,
    manager_for,
    refusing_runner,
)

from buildlimits import sccache as sccache_module
from buildlimits import startup
from buildlimits.config import SupervisorConfig
from buildlimits.supervisor import Supervisor
from buildlimits.systemd import UserManager


def test_the_barrier_survives_a_failed_startup_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unaccounted cache keeps its barrier and blocks the token pool."""
    monkeypatch.setattr(startup, "drain_slice", lambda *_args: 0)
    marker = tmp_path / "cache-recovery"
    marker.write_text("recovering\n", encoding="ascii")
    settings = SupervisorConfig(
        cache_mode="sccache", sccache_unit=CACHE_UNIT, recovery_marker=marker
    )
    supervisor = Supervisor(settings, UserManager(refusing_runner))
    with supervisor.selector:
        with pytest.raises(sccache_module.SccacheError):
            supervisor.start(OWN_CGROUP)
        assert supervisor.pool is None, "the token pool was never opened"
        assert marker.exists(), "the barrier outlives an unaccounted start-up"


def _restarted(
    owned: contextlib.ExitStack,
    tmp_path: Path,
    *,
    mode: str = "sccache",
    present: bool = True,
    refuse: str | None = None,
) -> tuple[Supervisor, CacheState, Path]:
    """Build a supervisor restarting over a stopped, empty managed cache.

    Parameters
    ----------
    owned : contextlib.ExitStack
        Closes the supervisor's descriptors when the test ends.
    tmp_path : Path
        The disposable fixture directory holding the barrier.
    mode : str
        The configured cache mode.
    present : bool
        Whether an interrupted transaction left its barrier behind.
    refuse : str or None
        The control verb the scripted manager refuses, or None for none.

    Returns
    -------
    tuple[Supervisor, CacheState, Path]
        The supervisor, its scripted cache and the barrier's path.
    """
    marker = tmp_path / "cache-recovery"
    if present:
        marker.write_text("recovering\n", encoding="ascii")
    state = CacheState(refuse=refuse, active=False)
    state.marker = marker
    settings = SupervisorConfig(
        cache_mode=mode, sccache_unit=CACHE_UNIT, recovery_marker=marker
    )
    supervisor = owned.enter_context(
        contextlib.closing(Supervisor(settings, manager_for(state)))
    )
    return supervisor, state, marker


@pytest.mark.parametrize(
    ("mode", "present", "fate"),
    [
        ("sccache", True, BarrierFate(remaining=False, starts=True)),
        ("sccache", False, BarrierFate(remaining=False, starts=False)),
        ("direct", True, BarrierFate(remaining=True, starts=False)),
    ],
    ids=["interrupted", "absent", "direct-mode"],
)
def test_an_accounted_startup_reclaims_only_its_own_barrier(
    owned: contextlib.ExitStack,
    tmp_path: Path,
    mode: str,
    present: bool,
    fate: BarrierFate,
) -> None:
    """The barrier clears, and the cache restarts, in the owning mode only."""
    supervisor, state, marker = _restarted(owned, tmp_path, mode=mode, present=present)
    supervisor.recovery.reclaim_after_accounting()
    assert marker.exists() is fate.remaining, "the barrier's fate follows the mode"
    assert state.verbs == (["start"] if fate.starts else []), "only the owner restarts"


def test_a_refused_reclaim_restart_is_reported_without_reblocking_the_cache(
    owned: contextlib.ExitStack,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cache that will not start is announced, and stays free to start.

    Accounting proved the cache stopped and empty, so the barrier is obsolete
    by then: keeping it would block the cache for ever over a start the
    manager merely refused once. The refusal is not fatal either, but the
    operator who sees no cache must be told why rather than left to infer it
    from a supervisor that quietly compiles directly.
    """
    supervisor, state, marker = _restarted(owned, tmp_path, refuse="start")
    supervisor.recovery.reclaim_after_accounting()
    assert not marker.exists(), "a refused start does not re-block the cache"
    assert "start" in state.verbs, "the restart was attempted"
    assert "cache start refused" in capsys.readouterr().err, (
        "the refusal reaches the operator's journal"
    )


def test_a_barrier_that_cannot_be_cleared_keeps_a_reclaimed_cache_from_starting(
    owned: contextlib.ExitStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reclamation reports a barrier it cannot clear, never claiming a start.

    A supervisor that died mid-transaction left the barrier, and its next
    start-up has just proved the cache stopped and empty. Accounting already
    succeeded, so the refusal is not fatal, but a start issued behind a
    barrier that is still there reaches a unit whose own condition skips it.
    The journal must carry the real reason.
    """
    supervisor, state, marker = _restarted(owned, tmp_path)
    unremovable_barrier(monkeypatch, marker, "Read-only file system")
    supervisor.recovery.reclaim_after_accounting()
    assert marker.exists(), "the barrier survives the refused removal"
    assert state.verbs == [], "no start is queued behind the surviving barrier"
    assert "barrier" in capsys.readouterr().err, (
        "the refusal reaches the operator's journal"
    )


def test_a_barrier_that_cannot_be_inspected_is_left_alone(
    owned: contextlib.ExitStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown barrier stops reclamation, rather than reading as absent.

    A tightened or unmounted runtime directory makes the barrier's presence
    unknown rather than false, and the reclaim path reads that from ``stat``
    because ``Path.exists`` reports such a path as absent on Python 3.14.
    Neither action is safe on an unknown: a start would be skipped by a unit
    condition that may still see a barrier, and a clear would fail on the same
    unreadable path. So nothing is attempted, and the operator is given the
    reason instead of a cache that silently never returns.
    """
    supervisor, state, marker = _restarted(owned, tmp_path)
    unreadable_barrier(monkeypatch, marker, "Permission denied")
    supervisor.recovery.reclaim_after_accounting()
    assert state.verbs == [], "no start is issued on an unreadable barrier"
    # Read the directory, not the path: the fixture breaks ``stat`` for the
    # barrier precisely because that is the failure under test, so asking a
    # path method here would only re-raise its error.
    present = {entry.name for entry in tmp_path.iterdir()}
    assert "cache-recovery" in present, "the barrier is left exactly as it was found"
    error = capsys.readouterr().err
    assert "cannot inspect" in error, "the failure is reported as an inspection one"
    assert "Permission denied" in error, "the operating-system reason is not discarded"


def test_an_unreadable_barrier_is_reported_even_where_exists_reports_absent(
    owned: contextlib.ExitStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An inspection failure is reported, not read as an absent barrier.

    This pins the reclaim path to ``stat`` rather than ``Path.exists``, and it
    fails against the earlier implementation on any interpreter. ``exists``
    answers ``False`` for every ``OSError`` from Python 3.14 onwards, so the
    older code returned early and silently on an unreadable runtime directory —
    the reasoning that it "raises, because the answer is unknown" held only
    through 3.13. The patch below states that 3.14 behaviour directly, so the
    assertion holds whatever interpreter runs the suite: if the reclaim path
    consults ``exists`` it silently does nothing and the journal stays empty.
    """
    supervisor, state, marker = _restarted(owned, tmp_path)
    unreadable_barrier(monkeypatch, marker, "Permission denied")

    def absent_on_error(self: Path, *, follow_symlinks: bool = True) -> bool:
        """Answer as Python 3.14's ``Path.exists`` does for a guarded path."""
        try:
            self.stat(follow_symlinks=follow_symlinks)
        except OSError:
            return False
        return True

    monkeypatch.setattr(Path, "exists", absent_on_error)
    supervisor.recovery.reclaim_after_accounting()
    assert state.verbs == [], "an unreadable directory never authorizes a start"
    assert "cannot inspect" in capsys.readouterr().err, (
        "the unknown path is reported rather than read as absent"
    )
