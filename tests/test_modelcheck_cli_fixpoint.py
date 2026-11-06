"""``--fixpoint`` as the command line exposes it: status, report and artefact.

The comparison is one command's option, so its tests live beside that command
rather than among the matrix's status tests. Three things are pinned here: a
stable comparison passes, a growing one exits unmet while every matrix row
held, and a comparison that fails after the matrix was saved is still written
into the artefact. The last is the regression for a file that claimed a clean
run the exit status contradicted.
"""

import json
import typing as typ
from pathlib import Path

import pytest
from modelcheck_artefacts import Record, doubled_failure, fixpoint_record
from modelcheck_cli_cases import (
    CHECK,
    BoundSensitive,
    app,
    bounded,
    checked,
    checked_in,
    checked_json,
)
from modelcheck_doubles import CounterexampleDoubled, FailsDoubled, FakeChecker

from modelcheck import cli, domain


def test_fixpoint_reports_a_stable_exploration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A count that does not move when the bound doubles is reported stable.

    The recorded double answers four states whatever bound it is given, which is
    what a run that reached its fixpoint looks like. Both counts are asserted
    against the same row so the report cannot pass by showing one number twice
    from two different configurations.
    """
    application = app([bounded()], FakeChecker("pass"))

    status, printed = checked(application, capsys, *CHECK, "--fixpoint")

    assert status == cli.EXIT_MET, "a stable exploration passes"
    assert "head" in printed and "4 -> 4 stable" in printed, (
        "the report shows both counts on the configuration's row"
    )


def test_fixpoint_growth_exits_unmet(capsys: pytest.CaptureFixture[str]) -> None:
    """A run that explored more at twice the bound was truncated, and fails.

    Every expectation was met, so the matrix's own status is ``0``. The
    fixpoint is what makes the bound part of the run's meaning: a pass at an
    exhausted bound proves only what the bound allowed.
    """
    application = app([bounded()], BoundSensitive())

    status, printed = checked(application, capsys, *CHECK, "--fixpoint")

    assert status == cli.EXIT_UNMET, "a truncated exploration is not a pass"
    assert "grew" in printed, "and the report says the count moved"


def test_a_growing_comparison_is_written_to_the_artefact(tmp_path: Path) -> None:
    """A growth result reaches the artefact, not only the exit status.

    The command exits ``1`` for a truncated exploration while every matrix row
    held, so a file holding only the rows would describe a clean run the status
    contradicts. The record carries both counts and both bounds, which is what
    lets a reader of the file see how far the exploration ran before the bound
    cut it off — a count alone cannot show that the second run was given a
    larger bound at all.
    """
    application = app([bounded()], BoundSensitive())

    status, records = checked_json(application, tmp_path, "--fixpoint")

    assert status == cli.EXIT_UNMET, "a truncated exploration is not a pass"
    (grown,) = [record for record in records if record["subject"] == "fixpoint"]
    assert grown["verdict"] == "grew", "the record says the count moved"
    assert grown["met"] is False, "a growing comparison is never met"
    assert (grown["states"], grown["doubled_states"]) == (8, 16), (
        "and carries both counts the comparison measured"
    )
    assert (grown["bound"], grown["doubled_bound"]) == (8, 16), (
        "with the bounds the two runs were given"
    )
    assert "elapsed_seconds" in grown and "doubled_elapsed_seconds" in grown, (
        "and each run's own duration"
    )


def test_a_failed_comparison_is_written_to_the_artefact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A comparison that fails after the matrix is saved is still recorded.

    The artefact is written before the fixpoint check runs, so a failure only
    the doubled-bound run produces would otherwise leave the file claiming a
    clean matrix. The record names the configuration, the stage and the bound,
    and carries the run's own diagnostic, which is what tells a truncated
    exploration from a broken model.
    """
    application = app([bounded()], FailsDoubled())
    path = tmp_path / "records.json"

    with pytest.raises(domain.MaterializeError, match="doubled bound"):
        application([
            "check",
            str(tmp_path),
            "--fizz",
            "/nonexistent/fizz",
            "--fixpoint",
            "--json",
            str(path),
        ])

    printed = capsys.readouterr().out
    assert "doubled bound failed" in printed, "the report names the failed run"
    records = typ.cast("list[Record]", json.loads(path.read_text(encoding="utf-8")))
    assert doubled_failure(records) == ("head", "doubled", 16, False), (
        "the record names the configuration, the stage and the bound that failed"
    )
    detail = typ.cast("str", fixpoint_record(records)["detail"])
    assert "doubled bound" in detail, "and its own diagnostic is kept"


def test_a_failed_comparison_saves_its_counterexample_trace(tmp_path: Path) -> None:
    """A comparison's counterexample is kept even without ``--json``.

    The matrix saves its own traces before the comparison runs, so a comparison
    that finds a counterexample at the doubled bound has nowhere to put it
    unless the failure path saves one too. The file name carries the stage,
    because the matrix's trace for the same configuration is a different run
    and must not be overwritten by this one.
    """
    application = app([bounded()], CounterexampleDoubled())

    with pytest.raises(domain.MaterializeError, match="doubled bound"):
        checked_in(application, tmp_path, "--fixpoint", "--save-traces")

    saved = tmp_path / "traces" / "head-fixpoint-doubled.json"
    assert saved.exists(), "the failed comparison's path is saved"
    record = json.loads(saved.read_text(encoding="utf-8"))
    assert record["ending"] == "lasso", "with the shape the run reported"
    assert record["steps"], "and the path it walked"
