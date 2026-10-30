"""The command line is a thin shell over the matrix, and its edge is pinned.

The command line is where a maintainer meets the tooling, so three things are
checked here that the service's own tests cannot see: which status each shape
of matrix exits with, that ``--only`` narrows the *report* without narrowing
the *check*, and that the JSON and trace artefacts say what the report says.
The app is built with adapter factories the test supplies, per ADR 005, so no
binary is started and no repository is read.

A usage error is asserted through ``cyclopts.CycloptsError`` rather than a
process exit, because ``make_app`` is built with ``exit_on_error=False``: the
module that decides what a failure prints is ``modelcheck.__main__``, and
letting the app exit here would end the test run instead of failing one test.
"""

import json
import typing as typ
from pathlib import Path

import cyclopts
import pytest
from modelcheck_cli_cases import SOURCE, Inventory, app, configuration, fails_as, holds
from modelcheck_doubles import FakeChecker, FixedSource

from modelcheck import cli, domain

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

_CHECK = ("check", ".", "--fizz", "/nonexistent/fizz")
"""A ``check`` command line over the current directory.

The launcher path is never opened: the app is built with a checker double, and
the path only has to be present for the option to parse.
"""


def _checked(
    application: cyclopts.App,
    capsys: pytest.CaptureFixture[str],
    *arguments: str,
) -> tuple[int, str]:
    """Run one command and return its status and what it printed.

    Parameters
    ----------
    application : cyclopts.App
        The app under test.
    capsys : pytest.CaptureFixture[str]
        The capture fixture, so the helper reads the report the command wrote.
    *arguments : str
        The command line, without a program name.

    Returns
    -------
    tuple[int, str]
        The exit status and the printed report, which are the two things every
        status test here asserts on.
    """
    status = application(list(arguments))
    return status, capsys.readouterr().out


def _checked_in(application: cyclopts.App, tmp_path: Path, *options: str) -> int:
    """Run a ``check`` over a scratch model directory and return its status.

    Parameters
    ----------
    application : cyclopts.App
        The app under test.
    tmp_path : pathlib.Path
        The model directory the run reads and writes its artefacts into.
    *options : str
        Further command-line options, such as ``--json`` or ``--save-traces``.

    Returns
    -------
    int
        The command's exit status.
    """
    return application(["check", str(tmp_path), *_CHECK[2:], *options])


def _files(directory: Path) -> list[str]:
    """Return the names a directory holds, in sorted order.

    Parameters
    ----------
    directory : pathlib.Path
        The directory to list.

    Returns
    -------
    list[str]
        The entry names, so an assertion reads as the file list a reviewer
        would see.
    """
    return sorted(path.name for path in directory.iterdir())


@pytest.mark.parametrize(
    ("checker_output", "status", "printed"),
    [
        pytest.param(
            "pass", cli.EXIT_MET, "model check: 1 expectation met, 0 unmet", id="met"
        ),
        pytest.param(
            "safety-failure", cli.EXIT_UNMET, "unexpected counterexample", id="unmet"
        ),
    ],
)
def test_the_exit_status_reports_the_matrix(
    capsys: pytest.CaptureFixture[str],
    checker_output: str,
    status: int,
    printed: str,
) -> None:
    """A met matrix exits ``0`` and an unmet one exits ``1``, saying which.

    An expectation met by every run is the command succeeding, and an
    unexpected counterexample is a failure rather than a warning: the status is
    what a script reads and the printed line is what a maintainer reads. They
    are asserted together because the damaging combination is a run that exits
    zero while reporting an expectation unmet — a gate that does not gate.
    """
    application = app([holds()], FakeChecker(checker_output))

    actual, output = _checked(application, capsys, *_CHECK)

    assert actual == status, "the status reports the matrix"
    assert printed in output, "and the report says which expectation"


def test_a_known_bad_configuration_that_still_fails_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The incident is reproduced, which is what the matrix is for."""
    application = app(
        [fails_as("incident-1", "L3")], FakeChecker("lasso", property_name="L3")
    )

    status, printed = _checked(application, capsys, *_CHECK)

    assert status == cli.EXIT_MET, "a known-bad configuration that still fails passes"
    assert "counterexample as expected" in printed, "and says why"


def test_only_narrows_the_report_but_not_the_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An omitted configuration still runs, and can still fail the command.

    Reporting only ``head`` would hide ``incident-1``'s lost counterexample if
    the selection also narrowed the exit status, so both are read from the same
    narrowed sequence — and that sequence is what the reader asked to see. The
    matrix itself runs in full either way.
    """
    application = app(
        [holds("head"), fails_as("incident-1", "L3")],
        FakeChecker("pass"),
    )

    status = application([
        "check",
        ".",
        "--fizz",
        "/nonexistent/fizz",
        "--only",
        "head",
    ])

    printed = capsys.readouterr().out
    assert status == cli.EXIT_MET, "the narrowed report still exits 0"
    assert "head" in printed, "the selected configuration is reported"
    assert "incident-1" not in printed, "and the omitted one is not"


def test_only_accepts_a_single_property(tmp_path: Path) -> None:
    """``CONFIG:PROPERTY`` selects one row of one configuration."""
    application = app(
        [configuration("head", (domain.Holds("S1"), domain.Holds("S2")))],
        FakeChecker("pass"),
    )

    status = application([
        "check",
        str(tmp_path),
        "--fizz",
        "/nonexistent/fizz",
        "--only",
        "head:S2",
        "--json",
        str(tmp_path / "records.json"),
    ])

    records = json.loads((tmp_path / "records.json").read_text(encoding="utf-8"))
    assert status == cli.EXIT_MET, "the property alone is selected"
    assert [record["subject"] for record in records] == ["S2"], "one row is written"


def test_the_json_holds_one_record_per_reported_judgement(
    tmp_path: Path, snapshot: SnapshotAssertion
) -> None:
    """The machine-readable form carries the same rows as the report.

    The record's field set is the contract a reader of ``--json`` writes against,
    so it is asserted whole rather than field by field: a rename or a dropped
    key is a change to that contract and should be reviewed as one.
    """
    application = app([holds("head", "S1")], FakeChecker("pass", property_name="S1"))

    application([
        "check",
        str(tmp_path),
        "--fizz",
        "/nonexistent/fizz",
        "--json",
        str(tmp_path / "records.json"),
    ])

    records = json.loads((tmp_path / "records.json").read_text(encoding="utf-8"))
    assert records == snapshot, "the record's field set is the contract"


@pytest.mark.parametrize(
    "options",
    [
        pytest.param((), id="no-json"),
        pytest.param(("--save-traces",), id="save-traces"),
    ],
)
def test_an_ordinary_run_writes_no_artefact(
    tmp_path: Path, options: tuple[str, ...]
) -> None:
    """A pass writes nothing unasked for, and no empty file either.

    ``--json`` is opt-in, so a run without it leaves the directory as it found
    it; and ``--save-traces`` over a pass has no path to save, where an empty
    trace file would claim a counterexample the run never produced. Both cases
    are the one promise — an artefact appears only when the run produced it —
    so they share a test and differ by the options that must still write
    nothing.
    """
    application = app([holds()], FakeChecker("pass"))

    _checked_in(application, tmp_path, *options)

    assert _files(tmp_path) == [], options


def test_save_traces_names_each_counterexample_after_its_row(
    tmp_path: Path,
) -> None:
    """One file per counterexample, named for its configuration and property.

    The name is the row's own identity rather than a timestamp, so a re-run
    replaces its trace and a review of the model directory sees which paths
    moved since the last run.
    """
    application = app(
        [fails_as("incident-1", "L3")], FakeChecker("lasso", property_name="L3")
    )

    application([
        "check",
        str(tmp_path),
        "--fizz",
        "/nonexistent/fizz",
        "--save-traces",
    ])

    assert _files(tmp_path / "traces") == ["incident-1-L3.json"], "one trace is saved"
    record = json.loads(
        (tmp_path / "traces" / "incident-1-L3.json").read_text(encoding="utf-8")
    )
    assert record["ending"] == "lasso", "the trace keeps its ending"
    assert record["steps"], "and the steps the counterexample walked"


def test_save_traces_writes_nothing_for_a_pass(tmp_path: Path) -> None:
    """A pass has no path to save, and an empty one would be a false trace."""
    application = app([holds()], FakeChecker("pass"))

    _checked_in(application, tmp_path, "--save-traces")

    assert _files(tmp_path) == [], "a pass has no trace to save"


def test_a_clean_drift_check_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """A model that maps every path the code has is not drifting."""
    application = app(
        [],
        FakeChecker(),
        inventory=frozenset({"buildlimits.admission.Admission.submit"}),
    )

    status = application(["drift", "."])

    assert status == cli.EXIT_MET, "a clean drift exits 0"
    assert "accounts for every production path" in capsys.readouterr().out, (
        "and the report says so"
    )


def test_a_drifted_model_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    """An effect the model never mapped is a finding, and fails the check."""
    application = app(
        [],
        FakeChecker(),
        inventory=frozenset({
            "buildlimits.admission.Admission.submit",
            "buildlimits.effects.Pause",
        }),
    )

    status = application(["drift", "."])

    printed = capsys.readouterr().out
    assert status == cli.EXIT_UNMET, "an unmapped path is drift"
    assert "unmapped: buildlimits.effects.Pause" in printed, "and is reported by name"


def test_a_renamed_path_is_drift() -> None:
    """A mapping that no longer resolves is reported as stale."""
    application = app(
        [],
        FakeChecker(),
        inventory=frozenset({"buildlimits.admission.Admission.submit"}),
        resolves=lambda _path: False,
    )

    assert application(["drift", "."]) == cli.EXIT_UNMET, "a stale path is drift"


def test_a_usage_error_raises_rather_than_exiting() -> None:
    """The caller decides what a usage error prints and what it exits with."""
    application = app([holds()], FakeChecker("pass"))

    with pytest.raises(cyclopts.CycloptsError):
        application(["check", "."])


def test_help_lists_both_commands(capsys: pytest.CaptureFixture[str]) -> None:
    """A reader checking the tool is wired learns what it can do."""
    application = app([holds()], FakeChecker("pass"))

    assert application(["--help"]) is None, "help returns no status to exit with"

    printed = capsys.readouterr().out
    assert "check" in printed, "the matrix command is listed"
    assert "drift" in printed, "and so is the drift command"


def test_bounds_are_passed_to_the_source_factory() -> None:
    """``--bounds nightly`` reaches the adapter rather than being ignored.

    The bound set selects which numbers a model's ``fizz.yaml`` declares, so a
    command that accepted the flag and dropped it would run the routine bounds
    under a nightly label and report the result as though it had been asked
    for.
    """
    seen: list[tuple[Path, str]] = []
    source = FixedSource([holds()], source=SOURCE)
    application = cli.make_app(
        lambda _path: FakeChecker("pass"),
        lambda directory, bounds: (seen.append((directory, bounds)), source)[1],
        Inventory,
    )

    application(["check", "/somewhere", "--fizz", "/fizz", "--bounds", "nightly"])

    assert seen == [(Path("/somewhere"), "nightly")], "the flag reaches the adapter"
