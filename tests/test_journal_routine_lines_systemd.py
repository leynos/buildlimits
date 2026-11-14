"""Real-journal evidence for routine supervisor lines and their priority."""

import contextlib
import typing as typ

import pytest
from harness_processes import journal_records
from systemd_harness import Harness, systemd_user_marks, wait_for

from buildlimits.journal_policy import DEBUG_PRIORITY, ROUTINE_LINES_VARIABLE

if typ.TYPE_CHECKING:
    import collections.abc as cabc

pytestmark = [
    *systemd_user_marks("needs a systemd user manager and busctl"),
    pytest.mark.timeout(180),
]

ADMISSION = "build-limits-supervisor: admitted request"
FINISH = "build-limits-supervisor: finished request"
# journald decodes the ``<N>`` prefix into a textual PRIORITY field.
ROUTINE_PRIORITY = DEBUG_PRIORITY.removeprefix("<").removesuffix(">")
NOTABLE_PRIORITY = "6"


@contextlib.contextmanager
def _supervisor(unit_env: dict[str, str]) -> cabc.Generator[Harness]:
    """Run one isolated supervisor with the given unit environment.

    Yields
    ------
    Harness
        The started harness, closed afterwards.
    """
    harness = Harness(1)
    harness.unit_env = dict(unit_env)
    try:
        harness.start()
        yield harness
    finally:
        harness.close()


def _of_kind(harness: Harness, prefix: str) -> list[dict[str, typ.Any]]:
    """Select the supervisor's journal lines that begin with ``prefix``.

    Returns
    -------
    list[dict[str, Any]]
        One decoded journal record per matching line, oldest first.
    """
    return [
        record
        for record in journal_records(harness.unit)
        if str(record.get("MESSAGE", "")).startswith(prefix)
    ]


def _run(harness: Harness, script: str) -> None:
    """Run one build to completion and wait for the supervisor to finish it."""
    build = harness.compile(script)
    build.wait(timeout=60)
    # The supervisor replies before it writes its diagnostic, so wait for the
    # metric record that proves the finish path reached its end.
    assert wait_for(lambda: bool(harness.read_metric_events("request_finished"))), (
        f"the supervisor never finished the request:\n{harness.journal()}"
    )


def test_a_healthy_finish_leaves_no_routine_line_by_default() -> None:
    """A successful compile is described by its metric records alone."""
    with _supervisor({}) as harness:
        _run(harness, "exit 0")
        assert not _of_kind(harness, ADMISSION), (
            "an admission is routine and stays out of the journal by default"
        )
        assert not _of_kind(harness, FINISH), (
            "a clean finish is routine and stays out of the journal by default"
        )


def test_a_requested_routine_line_reaches_the_journal_at_debug_priority() -> None:
    """``BUILD_LIMITS_ROUTINE_LINES=1`` restores both lines at priority 7."""
    with _supervisor({ROUTINE_LINES_VARIABLE: "1"}) as harness:
        _run(harness, "exit 0")
        admissions = _of_kind(harness, ADMISSION)
        finishes = _of_kind(harness, FINISH)
        assert len(admissions) == 1, "the requested admission reaches the journal"
        assert len(finishes) == 1, "the requested clean finish reaches the journal"
        priorities = {record["PRIORITY"] for record in (*admissions, *finishes)}
        assert priorities == {ROUTINE_PRIORITY}, (
            "systemd parsed the prefix as the priority, not as message text"
        )


def _assert_one_notable_finish(harness: Harness) -> None:
    """Assert the supervisor wrote exactly one finish line, at default priority."""
    finishes = _of_kind(harness, FINISH)
    assert len(finishes) == 1, "a failed finish is always written"
    assert finishes[0]["PRIORITY"] == NOTABLE_PRIORITY, (
        "a failure is not a routine line"
    )


@pytest.mark.parametrize(
    "unit_env",
    [{ROUTINE_LINES_VARIABLE: "1"}, {}],
    ids=["routine-lines-requested", "default"],
)
def test_a_failed_finish_is_written_and_notable(unit_env: dict[str, str]) -> None:
    """A failing compile keeps the default priority under either setting."""
    with _supervisor(unit_env) as harness:
        _run(harness, "exit 1")
        _assert_one_notable_finish(harness)
