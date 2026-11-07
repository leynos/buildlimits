"""The guard that reports cache processes no admitted scope covers.

The guard exists for one failure: kache starts its daemon from inside the
client when none is listening, so the daemon inherits the compiler's scope and
outlives the compile. A token returned for that emptied scope would then cover
work that is still running. ADR 006 answers with a daemon the supervisor never
starts, and this module reports anything that gets past that.

What is asserted here is the accounting contract rather than the reading
mechanics: a covered process is never reported, an uncovered one always is,
and a status query must not consume the journal line the next poll owes.
"""

import selectors
import subprocess
import typing as typ
from pathlib import Path

import pytest
from fake_cache_processes import kill_and_reap
from kache_guard_helpers import cache_processes, fake_scope
from kache_stray_fixture import named_kache_binary

from buildlimits import kache_guard
from buildlimits.cache_backend import CacheBackend, DirectCache
from buildlimits.config import SupervisorConfig
from buildlimits.groups import Groups, Running
from buildlimits.kache import Kache
from buildlimits.sccache import ManagedSccache

if typ.TYPE_CHECKING:
    from buildlimits.launch import HeldChild
    from buildlimits.systemd import UserManager

__all__ = ["cache_processes"]  # Re-exported so pytest finds the fixture here.


def _backend(cache_mode: str, *, in_scope: bool = True) -> CacheBackend:
    """Build the backend a given mode composes, or a stand-in of one capability."""
    if cache_mode == "kache":
        if in_scope:
            return Kache("/managed/kache")
        return ManagedSccache("stand-in.service", Path("/stand-in.sock"))
    if cache_mode == "sccache":
        return ManagedSccache("stand-in.service", Path("/stand-in.sock"))
    return DirectCache()


def _groups(
    selector: selectors.BaseSelector,
    cache_mode: str = "kache",
    *,
    in_scope: bool = True,
    slice_dir: Path | None = None,
) -> Groups:
    """Build a group registry with the backend a mode selects."""
    groups = Groups(
        SupervisorConfig(cache_mode=cache_mode),
        typ.cast("UserManager", object()),
        selector,
        lambda: 0.0,
        _backend(cache_mode, in_scope=in_scope),
    )
    groups.slice_dir = slice_dir
    return groups


def test_a_scope_admission_has_forgotten_is_not_covered(
    tmp_path: Path, cache_processes: set[int]
) -> None:
    """A process in a scope no group holds has outlived the token that covered it."""
    held = fake_scope(tmp_path, "held", [10, 11])
    fake_scope(tmp_path, "forgotten", [20, 21])
    cache_processes.update({10, 11, 20, 21})

    found = kache_guard.find(tmp_path, [held])

    assert found == (20, 21), "only the forgotten scope's members are uncovered"


def test_a_covered_process_is_never_reported(
    tmp_path: Path, cache_processes: set[int]
) -> None:
    """The daemon a live group owns is its own business while the group runs."""
    first = fake_scope(tmp_path, "first", [20, 21])
    second = fake_scope(tmp_path, "second", [21, 22])
    cache_processes.update({20, 21, 22})

    found = kache_guard.find(tmp_path, [first, second])
    assert not found, "the union of the held scopes covers every listed process"


def test_only_cache_processes_are_reported(
    tmp_path: Path, cache_processes: set[int]
) -> None:
    """A stray compiler is a different fault; this guard reports only kache."""
    scope = fake_scope(tmp_path, "forgotten", [30, 31])
    cache_processes.add(31)

    assert kache_guard.find(tmp_path, []) == (31,), (
        f"{scope} holds one cache process and one compiler"
    )


def test_readings_follow_scope_name_order(
    tmp_path: Path, cache_processes: set[int]
) -> None:
    """The report is stable, so a change of ordering is a real change."""
    fake_scope(tmp_path, "b-second", [42])
    fake_scope(tmp_path, "a-first", [41])
    cache_processes.update({41, 42})

    assert kache_guard.find(tmp_path, []) == (41, 42), "scopes are read in name order"


def test_an_unreadable_scope_does_not_fail_the_reading(
    tmp_path: Path, cache_processes: set[int]
) -> None:
    """A poll must not raise out of a diagnostic that only adds a journal line."""
    unreadable = tmp_path / "buildlimits-unreadable.scope"
    unreadable.mkdir()
    (unreadable / "cgroup.procs").mkdir()

    assert not kache_guard.find(tmp_path, []), "an unreadable scope reads as empty"


def test_an_unreadable_slice_does_not_fail_the_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slice can vanish between the existence check and the walk.

    ``slice_member_pids`` returns ``[]`` for a slice that is not there, but its
    ``walk`` is not guarded, so a slice removed in between raises. The guard
    only reports; a reading it cannot take is no strays, not a poll fault and
    not a failed status query.
    """
    slice_dir = tmp_path / "buildlimits-compilers.slice"
    slice_dir.mkdir()

    def _vanish(_path: Path) -> list[int]:
        msg = "the slice went away"
        raise OSError(msg)

    monkeypatch.setattr(kache_guard.cgroups, "slice_member_pids", _vanish)

    assert not kache_guard.find(slice_dir, []), "an unreadable slice reads as empty"


def test_a_process_in_a_foreign_scope_is_found(
    tmp_path: Path, cache_processes: set[int]
) -> None:
    """The guard must not rely on units carrying the supervisor's own name.

    ``buildlimits-compilers.slice`` can hold a transient service, another
    harness's scope, or any other unit. A reading of only the scopes this
    supervisor recognizes would never look inside them, so a kache daemon
    started there would run unaccounted for.
    """
    foreign = tmp_path / "some-service.service"
    foreign.mkdir()
    (foreign / "cgroup.procs").write_text("5150\n", encoding="ascii")
    cache_processes.add(5150)

    assert kache_guard.find(tmp_path, []) == (5150,), (
        "a scope the supervisor did not name is still inside its slice"
    )


def test_a_process_in_a_nested_slice_is_found(
    tmp_path: Path, cache_processes: set[int]
) -> None:
    """A slice nested under the compilers slice is read too, not just scopes."""
    nested = tmp_path / "buildlimits-compilers-extra.slice"
    inner = nested / "buildlimits-9-g1.scope"
    inner.mkdir(parents=True)
    (inner / "cgroup.procs").write_text("6160\n", encoding="ascii")
    cache_processes.add(6160)

    assert kache_guard.find(tmp_path, []) == (6160,), (
        "the whole slice is read, not one level of it"
    )


def test_an_admitted_scope_covers_only_its_own_members(
    tmp_path: Path, cache_processes: set[int]
) -> None:
    """Coverage is subtraction from the slice reading, not exclusion from it.

    A held group's own members are covered; a kache process in a neighbouring
    unit is not, even though both are read from the same slice in one pass.
    """
    held = fake_scope(tmp_path, "held", [7001, 7002])
    elsewhere = tmp_path / "some-service.service"
    elsewhere.mkdir()
    (elsewhere / "cgroup.procs").write_text("7003\n", encoding="ascii")
    cache_processes.update({7001, 7002, 7003})

    assert kache_guard.find(tmp_path, [held]) == (7003,), (
        "only the units the admitted scope does not cover are reported"
    )


def test_a_process_in_the_slice_itself_is_found(
    tmp_path: Path, cache_processes: set[int]
) -> None:
    """A process can sit directly in the slice, in no child scope at all.

    Reading only the scope directories would miss it, since it belongs to none
    of them. It is still inside ``buildlimits-compilers.slice`` and no token
    covers it, which is the whole of the guard's definition.
    """
    (tmp_path / "cgroup.procs").write_text("8002\n", encoding="ascii")
    cache_processes.add(8002)

    assert kache_guard.find(tmp_path, []) == (8002,), (
        "the slice's own members are read, not only its children's"
    )


@pytest.mark.parametrize(
    ("cache_mode", "in_scope", "expected"),
    [
        ("kache", True, True),
        ("kache", False, False),
        ("sccache", False, False),
        ("direct", True, False),
    ],
    ids=["in-scope-cache", "in-scope-mode-detached-adapter", "detached", "no-cache"],
)
def test_the_guard_is_gated_on_the_capability_not_the_mode(
    selector: selectors.BaseSelector,
    cache_mode: str,
    in_scope: bool,
    expected: bool,
) -> None:
    """Only a mode that is a cache *and* works in scope can leave a daemon behind."""
    groups = _groups(selector, cache_mode, in_scope=in_scope)
    assert kache_guard.guarded(groups) is expected, (
        "the read is skipped for every mode that cannot start a cache in a scope"
    )


def test_a_held_group_covers_its_scope(selector: selectors.BaseSelector) -> None:
    """Every group whose scope has not emptied is what admission still holds."""
    groups = _groups(selector)
    groups.running["g1"] = Running(
        "g1", 1, Path("/slice/buildlimits-g1.scope"), typ.cast("HeldChild", object())
    )
    groups.running["g2"] = Running(
        "g2", 2, Path("/slice/buildlimits-g2.scope"), typ.cast("HeldChild", object())
    )

    assert kache_guard.admitted_scopes(groups) == [
        Path("/slice/buildlimits-g1.scope"),
        Path("/slice/buildlimits-g2.scope"),
    ], "the covered scopes are exactly the running groups'"


def test_the_guard_finds_a_real_process_named_kache_that_it_was_shown(
    tmp_path: Path,
) -> None:
    """The reading recognizes a real process, not only a substituted predicate.

    Every other test here replaces ``_is_kache`` with a set membership, which
    exercises the accounting but leaves the executable test itself unproven
    against a live process. A copy of ``sleep`` placed under the name kache is
    the process the guard exists for: its own executable link is the evidence,
    so this test reads ``/proc`` for real.

    The test owns the process rather than having the fake wrapper abandon it.
    A child that outlives a parent which has exited is an orphan, and a host
    is entitled to reap those -- the hosted CI runner does, within a single
    poll of this suite. The guard does not care who the parent is or whether
    one is still running; it reads the slice and then ``/proc``, so the
    accounting under test is exactly what a process this test still owns
    exercises.
    """
    slice_dir = tmp_path / "buildlimits-compilers.slice"
    scope = slice_dir / "buildlimits-9-g1.scope"
    scope.mkdir(parents=True)
    binary = named_kache_binary(tmp_path)
    # The process is owned here and reaped in the ``finally`` below, which is
    # the same guarantee a ``with`` block gives; this shape keeps the guard
    # reads, which need the process alive, inside the same try.
    process = subprocess.Popen(  # pylint: disable=consider-using-with
        [str(binary), "300"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # A fixture cannot park its process in a real scope, so the test writes the
    # live identifier into the scope's member list. Everything after that point
    # is the production reading: the slice walk, then ``/proc``.
    (scope / "cgroup.procs").write_text(f"{process.pid}\n", encoding="ascii")
    try:
        assert kache_guard.find(slice_dir, []) == (process.pid,), (
            "a live kache process in an unadmitted scope was reported"
        )
        assert not kache_guard.find(slice_dir, [scope]), (
            "the same process is covered once a held scope accounts for it"
        )
    finally:
        kill_and_reap(process, timeout=10)
