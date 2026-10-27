"""The guard that reports cache processes no admitted scope covers.

The guard exists for one failure: kache starts its daemon from inside the
client when none is listening, so the daemon inherits the compiler's scope and
outlives the compile. A token returned for that emptied scope would then cover
work that is still running. ADR 004 answers with a daemon the supervisor never
starts, and this module reports anything that gets past that.

What is asserted here is the accounting contract rather than the reading
mechanics: a covered process is never reported, an uncovered one always is,
and a status query must not consume the journal line the next poll owes.
"""

import selectors
import typing as typ
from pathlib import Path

import pytest

from buildlimits import kache, kache_guard
from buildlimits.cache_backend import CacheBackend, DirectCache
from buildlimits.config import SupervisorConfig
from buildlimits.groups import Groups, Running
from buildlimits.kache import Kache
from buildlimits.sccache import ManagedSccache

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.launch import HeldChild
    from buildlimits.systemd import UserManager


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


def _fake_scope(slice_dir: Path, name: str, pids: cabc.Iterable[int]) -> Path:
    """Create a directory that looks like one supervisor-created scope."""
    scope = slice_dir / f"buildlimits-{name}.scope"
    scope.mkdir(parents=True)
    (scope / "cgroup.procs").write_text(
        "".join(f"{pid}\n" for pid in pids), encoding="ascii"
    )
    return scope


@pytest.fixture
def cache_processes(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """Treat every listed process as a kache executable for the test.

    The real test reads ``/proc/<pid>/exe``, which cannot name a fake process;
    the predicate is resolved through the module's globals at call time, so the
    substitution exercises the same decision the live guard makes.
    """
    is_kache: set[int] = set()
    monkeypatch.setattr(kache, "_is_kache", lambda pid: pid in is_kache)
    return is_kache


def _observed_stray(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cache_processes: set[int]
) -> tuple[kache_guard.Strays, list[str]]:
    """Leave one uncovered scope holding a cache process and capture the journal.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        The test's patcher, used to replace the module-level log sink.
    tmp_path : Path
        The slice directory for this test.
    cache_processes : set[int]
        The processes the fixture's predicate calls kache.

    Returns
    -------
    tuple[kache_guard.Strays, list[str]]
        A reading that has not observed the slice yet, and the lines the guard
        journals from now on, in order.
    """
    _fake_scope(tmp_path, "forgotten", [4242])
    cache_processes.update({4242})
    lines: list[str] = []
    monkeypatch.setattr(kache_guard, "log", lines.append)
    return kache_guard.Strays(), lines


def test_a_scope_admission_has_forgotten_is_not_covered(
    tmp_path: Path, cache_processes: set[int]
) -> None:
    """A process in a scope no group holds has outlived the token that covered it."""
    held = _fake_scope(tmp_path, "held", [10, 11])
    _fake_scope(tmp_path, "forgotten", [20, 21])
    cache_processes.update({10, 11, 20, 21})

    found = kache_guard.find(tmp_path, [held])

    assert found == (20, 21), "only the forgotten scope's members are uncovered"


def test_a_covered_process_is_never_reported(
    tmp_path: Path, cache_processes: set[int]
) -> None:
    """The daemon a live group owns is its own business while the group runs."""
    first = _fake_scope(tmp_path, "first", [20, 21])
    second = _fake_scope(tmp_path, "second", [21, 22])
    cache_processes.update({20, 21, 22})

    found = kache_guard.find(tmp_path, [first, second])
    assert not found, "the union of the held scopes covers every listed process"


def test_only_cache_processes_are_reported(
    tmp_path: Path, cache_processes: set[int]
) -> None:
    """A stray compiler is a different fault; this guard reports only kache."""
    scope = _fake_scope(tmp_path, "forgotten", [30, 31])
    cache_processes.add(31)

    assert kache_guard.find(tmp_path, []) == (31,), (
        f"{scope} holds one cache process and one compiler"
    )


def test_readings_follow_scope_name_order(
    tmp_path: Path, cache_processes: set[int]
) -> None:
    """The report is stable, so a change of ordering is a real change."""
    _fake_scope(tmp_path, "b-second", [42])
    _fake_scope(tmp_path, "a-first", [41])
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
    """The slice can vanish between the existence check and the listing.

    ``child_scopes`` returns ``[]`` for a slice that is not there, but its
    ``iterdir`` is not guarded, so a slice removed in between raises. The guard
    only reports; a reading it cannot take is no strays, not a poll fault and
    not a failed status query.
    """
    slice_dir = tmp_path / "buildlimits-compilers.slice"
    slice_dir.mkdir()

    def _vanish(_path: Path) -> list[Path]:
        raise OSError("the slice went away")

    monkeypatch.setattr(kache_guard.cgroups, "child_scopes", _vanish)

    assert not kache_guard.find(slice_dir, []), "an unreadable slice reads as empty"


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


def test_a_stray_is_journalled_once_per_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cache_processes: set[int],
) -> None:
    """A daemon that stays behind is named once, not on every poll."""
    strays, lines = _observed_stray(monkeypatch, tmp_path, cache_processes)

    strays.refresh(tmp_path, [])
    strays.refresh(tmp_path, [])

    assert len(lines) == 1, "two identical readings journal one line"
    assert "4242" in lines[0], f"the line names the process: {lines[0]}"


def test_a_cleared_stray_is_journalled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cache_processes: set[int],
) -> None:
    """An operator needs to know the fault is over, not only that it began."""
    strays, lines = _observed_stray(monkeypatch, tmp_path, cache_processes)
    members = tmp_path / "buildlimits-forgotten.scope" / "cgroup.procs"

    for reading in ("4242\n", ""):
        members.write_text(reading, encoding="ascii")
        strays.refresh(tmp_path, [])

    assert len(lines) == 2, "the clearing is a change and is journalled"
    assert lines[1] == "no stray kache process remains outside the admitted scopes", (
        lines[1]
    )


def test_a_quiet_reading_is_not_journalled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A guard that has never found anything says nothing at all."""
    lines: list[str] = []
    monkeypatch.setattr(kache_guard, "log", lines.append)

    kache_guard.Strays().refresh(tmp_path, [])

    assert not lines, "silence before the first stray is the established behaviour"


def test_a_status_read_does_not_silence_the_next_journal_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cache_processes: set[int],
) -> None:
    """Answering one status query must not consume the poll's report.

    Status and the poll read the same slice, and status may be asked for at any
    moment. If a status read advanced the journalled baseline, an operator
    asking what the fault is would be the reason nobody ever saw it announced.
    """
    strays, lines = _observed_stray(monkeypatch, tmp_path, cache_processes)

    assert strays.observe(tmp_path, []) == (4242,), "status sees the stray at once"
    assert not lines, "status itself never journals"
    strays.refresh(tmp_path, [])

    assert len(lines) == 1, "the poll still owes the line the status read did not pay"


def test_no_slice_yet_reads_as_no_strays() -> None:
    """Before the restart barrier there is nothing to read and nothing to report."""
    strays = kache_guard.Strays()

    assert not strays.observe(None, []), "an unknown slice is not an empty one"
    assert not strays.describe(), "status reports no strays before the slice is known"
