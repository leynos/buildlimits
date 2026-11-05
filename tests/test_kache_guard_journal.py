"""When the stray-process guard journals what it found.

:mod:`test_kache_guard` covers the reading: which processes a walk reports and
which scopes cover them. This module covers the accounting layered over it --
a change is journalled once, a status read never pays the line the poll owes,
and an unknown slice is not an empty one. A sibling module keeps both under the
repository's module-size ceiling.
"""

import typing as typ
from pathlib import Path

from kache_guard_helpers import cache_processes, observed_stray

from buildlimits import kache_guard

if typ.TYPE_CHECKING:
    import pytest

__all__ = ["cache_processes"]  # Re-exported so pytest finds the fixture here.


def test_a_stray_is_journalled_once_per_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cache_processes: set[int],
) -> None:
    """A daemon that stays behind is named once, not on every poll."""
    strays, lines = observed_stray(monkeypatch, tmp_path, cache_processes)

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
    strays, lines = observed_stray(monkeypatch, tmp_path, cache_processes)
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
    strays, lines = observed_stray(monkeypatch, tmp_path, cache_processes)

    assert strays.observe(tmp_path, []) == (4242,), "status sees the stray at once"
    assert not lines, "status itself never journals"
    strays.refresh(tmp_path, [])

    assert len(lines) == 1, "the poll still owes the line the status read did not pay"


def test_no_slice_yet_reads_as_no_strays() -> None:
    """Before the restart barrier there is nothing to read and nothing to report."""
    strays = kache_guard.Strays()

    assert not strays.observe(None, []), "an unknown slice is not an empty one"
    assert not strays.describe(), "status reports no strays before the slice is known"
