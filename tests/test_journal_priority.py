"""Routine supervisor lines are opt-in, and debug priority on a journal stream."""

import contextlib
import os
import typing as typ

import pytest
from request_helpers import launch_pending

from buildlimits import journal
from buildlimits.effects import Exit

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


def _request_routine_lines(
    monkeypatch: pytest.MonkeyPatch, setting: str | None
) -> None:
    """Set ``BUILD_LIMITS_ROUTINE_LINES``, or remove it when ``setting`` is ``None``."""
    if setting is None:
        monkeypatch.delenv(journal.ROUTINE_LINES_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(journal.ROUTINE_LINES_VARIABLE, setting)


@contextlib.contextmanager
def _journal_stderr(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> cabc.Generator[None]:
    """Point stderr at a file that ``JOURNAL_STREAM`` names, as systemd does.

    Routine lines are requested too. Pytest replaces ``sys.stderr`` for each
    test phase, so a test enters this itself rather than through a fixture.

    Yields
    ------
    None
        Control while stderr is the stand-in journal stream.
    """
    _request_routine_lines(monkeypatch, "1")
    with path.open("w") as stream:
        status = os.fstat(stream.fileno())
        monkeypatch.setenv("JOURNAL_STREAM", f"{status.st_dev}:{status.st_ino}")
        monkeypatch.setattr("sys.stderr", stream)
        yield


@pytest.mark.parametrize(
    ("routine", "expected"),
    [
        (True, "<7>build-limits-supervisor: done\n"),
        (False, "build-limits-supervisor: done\n"),
    ],
    ids=["routine", "notable"],
)
def test_only_routine_lines_carry_the_debug_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, routine: bool, expected: str
) -> None:
    """A unit with ``LogLevelMax=info`` keeps every line that is not routine."""
    path = tmp_path / "stream"
    with _journal_stderr(path, monkeypatch):
        journal.log("done", routine=routine)
    assert path.read_text() == expected, "priority follows the line's kind"


@pytest.mark.parametrize(
    "stream",
    [None, "", "not-a-stream", "1:1"],
    ids=["unset", "empty", "malformed", "other-file"],
)
def test_a_terminal_or_file_never_sees_a_priority_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: str | None
) -> None:
    """Without a matching journal stream the line is written unchanged."""
    _request_routine_lines(monkeypatch, "1")
    if stream is None:
        monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    else:
        monkeypatch.setenv("JOURNAL_STREAM", stream)
    path = tmp_path / "stderr"
    with path.open("w") as stderr:
        monkeypatch.setattr("sys.stderr", stderr)
        journal.log("done", routine=True)
    assert path.read_text() == "build-limits-supervisor: done\n", (
        "only systemd's own stream understands the prefix"
    )


@pytest.mark.parametrize(
    ("ending", "routine"),
    [
        ((Exit(code=0), None), True),
        ((Exit(code=1), None), False),
        ((Exit(signal=15), None), False),
        ((Exit(code=0), "No such file or directory"), False),
    ],
    ids=["succeeded", "failed", "signalled", "exec-failed"],
)
def test_only_successful_finishes_are_routine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: tuple[Exit, str | None],
    *,
    routine: bool,
) -> None:
    """Failures stay visible at the default priority; successes are debug."""
    exit_, exec_error = ending
    entry = launch_pending(("rustc",))
    entry.group_id = "g3"
    entry.exec_error = exec_error
    path = tmp_path / "stream"
    with _journal_stderr(path, monkeypatch):
        journal.log_finished(entry, 1.25, exit_)
    line = path.read_text()
    assert line.startswith("<7>") is routine, "only a clean exit is routine"
    assert line.removeprefix("<7>").startswith(
        "build-limits-supervisor: finished request 1 (g3) after 1.2s: "
    ), "the line's wording is unchanged"


@pytest.mark.parametrize("setting", [None, "", "0", "yes"], ids=str)
def test_routine_lines_are_omitted_unless_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str | None
) -> None:
    """Only ``1`` requests routine lines; notable lines are always written."""
    path = tmp_path / "stream"
    with _journal_stderr(path, monkeypatch):
        _request_routine_lines(monkeypatch, setting)
        for message, is_routine in (("routine", True), ("notable", False)):
            journal.log(message, routine=is_routine)
    assert path.read_text() == "build-limits-supervisor: notable\n", (
        "the metric records already hold what a routine line says"
    )
