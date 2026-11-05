"""Fixtures and builders shared by the stray-process guard's test modules.

:mod:`test_kache_guard` covers what a reading finds; its sibling covers when a
reading is journalled. Both need the same fake slice and the same substitution
for the executable test, so those live here rather than being copied between
the two.
"""

import typing as typ

import pytest

from buildlimits import kache_guard

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


def fake_scope(slice_dir: Path, name: str, pids: cabc.Iterable[int]) -> Path:
    """Create a directory that looks like one supervisor-created scope.

    Parameters
    ----------
    slice_dir : Path
        The slice the scope belongs to.
    name : str
        The scope's distinguishing name, used in its unit name.
    pids : Iterable[int]
        The process identifiers the scope lists as members.

    Returns
    -------
    Path
        The scope directory.
    """
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

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        The test's patcher, used to replace the module-level predicate.

    Returns
    -------
    set[int]
        The processes the test may add to, which its reading calls kache.
    """
    is_kache: set[int] = set()
    monkeypatch.setattr(kache_guard, "_is_kache", lambda pid: pid in is_kache)
    return is_kache


def observed_stray(
    monkeypatch: pytest.MonkeyPatch, slice_dir: Path, cache_processes: set[int]
) -> tuple[kache_guard.Strays, list[str]]:
    """Leave one uncovered scope holding a cache process and capture the journal.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        The test's patcher, used to replace the module-level log sink.
    slice_dir : Path
        The slice directory for this test.
    cache_processes : set[int]
        The processes the fixture's predicate calls kache.

    Returns
    -------
    tuple[kache_guard.Strays, list[str]]
        A reading that has not observed the slice yet, and the lines the guard
        journals from now on, in order.
    """
    fake_scope(slice_dir, "forgotten", [4242])
    cache_processes.update({4242})
    lines: list[str] = []
    monkeypatch.setattr(kache_guard, "log", lines.append)
    return kache_guard.Strays(), lines
