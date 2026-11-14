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
    JournalStream,
    RoutineLines,
    RoutineLineWriter,
    is_routine_finish,
    journal_stream,
    journal_writer,
    resolve_routine_lines,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def _journal_environ(
    destination: typ.TextIO, routine_lines: str | None = None
) -> dict[str, str]:
    """Name ``destination`` as the journal stream systemd connected.

    Parameters
    ----------
    destination : IO[str]
        The stream systemd is to be said to have connected.
    routine_lines : str | None
        The routine-line switch to include; omitted when ``None``.

    Returns
    -------
    dict[str, str]
        An environment naming the stream's device and inode.
    """
    status = os.fstat(destination.fileno())
    environ = {JOURNAL_STREAM_VARIABLE: f"{status.st_dev}:{status.st_ino}"}
    if routine_lines is not None:
        environ[ROUTINE_LINES_VARIABLE] = routine_lines
    return environ


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
        assert journal_stream(destination, environ) is JournalStream.OTHER, (
            "only systemd's own stream understands the prefix"
        )


def test_a_stream_without_a_descriptor_is_reported_as_uninspectable() -> None:
    """A replaced standard error names no device or inode to compare."""
    assert journal_stream(io.StringIO(), {"JOURNAL_STREAM": "1:1"}) is (
        JournalStream.UNINSPECTABLE
    ), "a stream that cannot be inspected is not silently called a non-journal"


def test_a_closed_stream_is_reported_as_uninspectable(tmp_path: Path) -> None:
    """A closed descriptor answers with an error rather than a device and inode."""
    with (tmp_path / "stderr").open("w") as destination:
        pass
    assert journal_stream(destination, {"JOURNAL_STREAM": "1:1"}) is (
        JournalStream.UNINSPECTABLE
    ), "a closed stream is uninspectable, not a crash at start-up"


def test_the_named_device_and_inode_identify_the_journal_stream(
    tmp_path: Path,
) -> None:
    """The stream systemd connected carries the priority prefix."""
    with (tmp_path / "stderr").open("w") as destination:
        assert journal_stream(destination, _journal_environ(destination)) is (
            JournalStream.JOURNAL
        ), "systemd's own stream is the one it named"


def test_a_named_journal_stream_resolves_the_debug_prefix(tmp_path: Path) -> None:
    """The resolved setting carries the prefix only on systemd's own stream."""
    with (tmp_path / "stderr").open("w") as destination:
        lines = resolve_routine_lines(_journal_environ(destination, "1"), destination)
    assert lines == RoutineLines(enabled=True, debug_prefix=DEBUG_PRIORITY), (
        "a requested line on the journal stream is resolved with the prefix"
    )


def test_an_uninspectable_stream_resolves_without_a_prefix() -> None:
    """The fail-safe keeps the line visible rather than printing a literal tag."""
    environ = {JOURNAL_STREAM_VARIABLE: "1:1", ROUTINE_LINES_VARIABLE: "1"}
    lines = resolve_routine_lines(environ, io.StringIO())
    assert lines == RoutineLines(enabled=True, debug_prefix=""), (
        "a stream that cannot be inspected is written to without a prefix"
    )


def test_the_prefix_and_the_line_share_one_stream(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A decision about one stream never governs a line written to another."""
    with (tmp_path / "stderr").open("a+") as destination:
        writer = journal_writer(_journal_environ(destination, "1"), destination)
        writer.write("admitted request 1", routine=True)
        destination.seek(0)
        written = destination.read()
    assert written == (
        f"{DEBUG_PRIORITY}build-limits-supervisor: admitted request 1\n"
    ), "the line reaches the stream whose device and inode decided the prefix"
    assert not capsys.readouterr().err, "no part of the line leaks to stderr"


@pytest.mark.parametrize(
    ("routine", "expected"),
    [
        (True, f"{DEBUG_PRIORITY}build-limits-supervisor: done\n"),
        (False, "build-limits-supervisor: done\n"),
    ],
    ids=["routine", "notable"],
)
def test_only_routine_lines_carry_the_debug_priority(
    *, routine: bool, expected: str
) -> None:
    """A unit with ``LogLevelMax=info`` keeps every line that is not routine."""
    out = io.StringIO()
    RoutineLineWriter(RoutineLines(True, DEBUG_PRIORITY), out).write(
        "done", routine=routine
    )
    assert out.getvalue() == expected, "priority follows the line's kind"


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
    setting: str | None, expected: str
) -> None:
    """Only ``1`` requests routine lines; notable lines are always written."""
    environ = {} if setting is None else {ROUTINE_LINES_VARIABLE: setting}
    out = io.StringIO()
    writer = journal_writer(environ, out)
    for message, is_routine in (("routine", True), ("notable", False)):
        writer.write(message, routine=is_routine)
    assert out.getvalue() == expected, (
        "the metric records already hold what a routine line says"
    )
