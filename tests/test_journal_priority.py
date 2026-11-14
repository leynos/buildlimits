"""Routine supervisor lines are opt-in, and debug priority on a journal stream."""

import io
import os
import typing as typ

import pytest
from request_helpers import launch_pending

from buildlimits import journal
from buildlimits.effects import Exit
from buildlimits.journal_policy import (
    DEBUG_PRIORITY,
    JOURNAL_STREAM_VARIABLE,
    ROUTINE_LINES_VARIABLE,
    RoutineLines,
    is_routine_finish,
    stream_is_journal,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def _assert_wrote(capsys: pytest.CaptureFixture[str], expected: str, why: str) -> None:
    """Assert that standard error holds exactly ``expected`` for ``why``."""
    assert capsys.readouterr().err == expected, why


@pytest.mark.parametrize(
    "stream",
    [None, "", "not-a-stream", "1:1"],
    ids=["unset", "empty", "malformed", "other-file"],
)
def test_a_terminal_or_file_never_sees_a_priority_prefix(
    tmp_path: Path, stream: str | None
) -> None:
    """Without a matching journal stream the line is written unchanged."""
    environ = {} if stream is None else {"JOURNAL_STREAM": stream}
    with (tmp_path / "stderr").open("w") as destination:
        assert not stream_is_journal(destination, environ), (
            "only systemd's own stream understands the prefix"
        )


def test_a_stream_without_a_descriptor_is_never_the_journal() -> None:
    """A replaced standard error has no device or inode to compare."""
    assert not stream_is_journal(io.StringIO(), {"JOURNAL_STREAM": "1:1"}), (
        "a stream that cannot be inspected is not systemd's"
    )


def test_the_named_device_and_inode_identify_the_journal_stream(
    tmp_path: Path,
) -> None:
    """The stream systemd connected carries the priority prefix."""
    with (tmp_path / "stderr").open("w") as destination:
        status = os.fstat(destination.fileno())
        environ = {"JOURNAL_STREAM": f"{status.st_dev}:{status.st_ino}"}
        assert stream_is_journal(destination, environ), (
            "systemd's own stream is the one it named"
        )


def test_a_named_journal_stream_resolves_the_debug_prefix(tmp_path: Path) -> None:
    """The resolved setting carries the prefix only on systemd's own stream."""
    with (tmp_path / "stderr").open("w") as destination:
        status = os.fstat(destination.fileno())
        environ = {
            JOURNAL_STREAM_VARIABLE: f"{status.st_dev}:{status.st_ino}",
            ROUTINE_LINES_VARIABLE: "1",
        }
        lines = RoutineLines.from_environment(environ, destination)
    assert lines == RoutineLines(enabled=True, debug_prefix=DEBUG_PRIORITY), (
        "a requested line on the journal stream is resolved with the prefix"
    )


@pytest.mark.parametrize(
    ("routine", "expected"),
    [
        (True, f"{DEBUG_PRIORITY}build-limits-supervisor: done\n"),
        (False, "build-limits-supervisor: done\n"),
    ],
    ids=["routine", "notable"],
)
def test_only_routine_lines_carry_the_debug_priority(
    capsys: pytest.CaptureFixture[str], *, routine: bool, expected: str
) -> None:
    """A unit with ``LogLevelMax=info`` keeps every line that is not routine."""
    lines = RoutineLines(enabled=True, debug_prefix=DEBUG_PRIORITY)
    lines.write("done", routine=routine)
    _assert_wrote(capsys, expected, "priority follows the line's kind")


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
    ending: tuple[Exit, str | None], *, routine: bool
) -> None:
    """Failures stay visible at the default priority; successes are debug."""
    exit_, exec_error = ending
    entry = launch_pending(("rustc",))
    entry.group_id = "g3"
    entry.exec_error = exec_error
    assert is_routine_finish(entry, exit_) is routine, (
        "only a clean exit repeats what the metric records hold"
    )


def test_a_finished_line_keeps_its_established_wording() -> None:
    """Classification never changes what the operator reads."""
    entry = launch_pending(("rustc",))
    entry.group_id = "g3"
    entry.exec_error = "No such file or directory"
    assert journal.finished_line(entry, 1.25, Exit(code=None, signal=15)) == (
        "finished request 1 (g3) after 1.2s: code=None signal=15"
    ), "the line's wording is unchanged"


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        (None, "build-limits-supervisor: notable\n"),
        ("", "build-limits-supervisor: notable\n"),
        ("0", "build-limits-supervisor: notable\n"),
        ("yes", "build-limits-supervisor: notable\n"),
        (
            "1",
            "build-limits-supervisor: routine\nbuild-limits-supervisor: notable\n",
        ),
    ],
    ids=["unset", "empty", "zero", "affirmative", "enabled"],
)
def test_routine_lines_are_omitted_unless_requested(
    capsys: pytest.CaptureFixture[str], setting: str | None, expected: str
) -> None:
    """Only ``1`` requests routine lines; notable lines are always written."""
    environ = {} if setting is None else {ROUTINE_LINES_VARIABLE: setting}
    lines = RoutineLines.from_environment(environ, io.StringIO())
    for message, is_routine in (("routine", True), ("notable", False)):
        lines.write(message, routine=is_routine)
    _assert_wrote(
        capsys, expected, "the metric records already hold what a routine line says"
    )
