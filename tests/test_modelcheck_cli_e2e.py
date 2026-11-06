"""The command line as the maintainer runs it, with a stub in place of the binary.

Every other command-line test builds the app in-process and hands it doubles.
That leaves three things unproven, and this module is here for exactly those:

- ``python -m modelcheck`` starts at all, which means the real adapter wiring in
  :mod:`modelcheck.__main__` is exercised rather than bypassed;
- a *relative* ``--fizz`` path works. The adapter runs the checker inside a
  scratch directory, so a launcher path resolved at run time would be looked
  for there; the end-to-end run is what found that, and it is why the path is
  resolved when the adapter is built;
- the run leaves nothing unintended in the tracked tree. ``.model-check/`` is
  git-ignored and each run's directory is removed, and ``--save-traces`` writes
  only the declared counterexample paths, so a run without that flag leaves
  ``git status --porcelain`` empty — the plan's constraint on every checker
  run.

The stub is a shell script in the fixture directory. It writes nothing there:
its record of what it was asked goes to the path ``MODELCHECK_E2E_LOG`` names,
which is a scratch directory the test owns. That is what lets the scratch-path
assertion be made at all without the test itself dirtying the tree it is
guarding.

``uv run`` is used rather than the current interpreter because it is the way a
maintainer invokes the tool, and because it is the invocation the plan names.
"""

import json
import shutil
import subprocess
import typing as typ
from pathlib import Path

from modelcheck_artefacts import Record, doubled_failure, fixpoint_record
from modelcheck_e2e_cases import (
    FIXTURES,
    FIZZ,
    MODEL,
    TIMEOUT_SECONDS,
    ambient,
    check,
    contents,
    invocations,
    run,
    tree,
)

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


def test_a_met_matrix_exits_zero(tmp_path: Path) -> None:
    """The whole matrix, run as a command, reports every expectation met.

    The two configurations are answered differently by the stub: the healthy
    one holds, and the incident is still reproduced. A run that reported only
    the first would pass this matrix whether or not the model still found the
    incident, so the second line is the one that makes the exit status mean
    something.
    """
    log = tmp_path / "seen.log"

    result = check(log=log)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == (
        "model check: 3 expectations met, 0 unmet"
    ), "the summary counts every expectation the matrix ran"
    assert "incident-1" in result.stdout, "the incident's row is reported"
    assert "counterexample as expected" in result.stdout, (
        "and it is still reproduced rather than silently fixed"
    )


def test_the_stub_runs_in_a_scratch_directory(tmp_path: Path) -> None:
    """Every run happens under ``.model-check/``, never beside the model.

    The stub records its own working directory rather than trusting the
    adapter's intent, and the paths are read back after the run. A relative
    ``--fizz`` that the adapter resolved at run time would not even be found
    here, which is the defect this test found; the specification it is handed
    is the scratch copy's bare file name for the same reason.
    """
    log = tmp_path / "seen.log"

    check(log=log)

    seen = invocations(log)
    assert len(seen) == 2, "one run per configuration"
    for directory, arguments in seen:
        working_directory = Path(directory)
        assert working_directory.parent.name == ".model-check", (
            "the checker runs under the scratch directory"
        )
        assert not working_directory.is_relative_to(MODEL), (
            "and never inside the model directory"
        )
        assert arguments.endswith(" model.fizz"), (
            "the specification is handed over by its scratch-copy name"
        )
        assert not working_directory.exists(), "the run cleaned up after itself"


def test_the_json_record_names_every_expectation(
    tmp_path: Path, snapshot: SnapshotAssertion
) -> None:
    """``--json`` writes one record per expectation, with its verdict.

    The records are the machine-readable form of the report, so the columns
    this test cares about are pinned whole rather than counted: a record whose
    ``met`` disagreed with its ``verdict`` would tell CI the opposite of what
    the console said. The ``states`` column is asserted beside the snapshot
    because it is the one field the run itself decides.
    """
    log = tmp_path / "seen.log"
    records = tmp_path / "matrix.json"

    result = check("--json", str(records), log=log)

    assert result.returncode == 0, result.stderr
    written = json.loads(records.read_text(encoding="utf-8"))
    projected = [
        (
            record["configuration"],
            record["subject"],
            record["verdict"],
            record["met"],
            record["fairness"],
        )
        for record in written
    ]
    assert projected == snapshot, "every record carries its own verdict"
    assert [record["states"] for record in written] == [4, 4, 4], (
        "and the count the stub reported"
    )


def test_a_lost_counterexample_exits_one(tmp_path: Path) -> None:
    """A defect that is gone is a matrix that failed, not a matrix that passed.

    The stub is told to answer a pass to every run, which is what a change that
    fixed the incident would look like. The configuration that exists to
    reproduce it has nothing left to find, so its expectation is unmet and the
    exit status is the failing one.
    """
    log = tmp_path / "seen.log"

    result = check(log=log, MODELCHECK_E2E_ANSWER="pass")

    assert result.returncode == 1, result.stderr
    assert "expected counterexample missing" in result.stdout, (
        "the lost defect is named as the unmet expectation"
    )
    assert result.stdout.splitlines()[-1] == (
        "model check: 2 expectations met, 1 unmet"
    ), "and the summary counts it as unmet"


def test_the_run_leaves_the_fixture_tree_untouched(tmp_path: Path) -> None:
    """Nothing the command line runs writes into the tracked fixture directory.

    The stub would happily leave its record beside itself, which is why it is
    given a log path elsewhere. This asserts the result rather than the
    intention: the fixture tree is the same set of paths afterwards, so a run
    that started writing there would be caught wherever it did it.
    """
    log = tmp_path / "seen.log"
    before = tree()

    check("--json", str(tmp_path / "matrix.json"), log=log)
    check(log=log, MODELCHECK_E2E_ANSWER="pass")

    assert tree() == before, "the fixture tree holds the same paths afterwards"
    assert not (FIXTURES / "seen.log").exists(), (
        "and the stub's record went to the scratch directory it was given"
    )


def test_a_run_writes_only_the_traces_it_was_asked_for(tmp_path: Path) -> None:
    """The one addition a run may make to a model directory is a declared trace.

    The plan's cleanliness requirement is about unintended writes, not about
    ``--save-traces``: that flag exists to keep each counterexample's path as
    reviewable evidence, so those files are the only addition a run may make.
    The model is copied first, because the fixture tree is tracked and this
    check is about what the run wrote rather than about the fixture.

    The comparison is by content, not by name. A listing of paths cannot tell a
    file the run rewrote from one it left alone, nor a file it deleted if the
    name reappears: the model's own files must be byte-identical afterwards.
    """
    model = tmp_path / "model"
    shutil.copytree(MODEL, model)
    before = contents(model)
    fixture_before = tree()
    log = tmp_path / "seen.log"

    result = run("check", str(model), "--fizz", str(FIZZ), "--save-traces", log=log)

    assert result.returncode == 0, result.stderr
    after = contents(model)
    assert sorted(set(after) - set(before)) == [
        Path("traces/incident-1-BEventually.json"),
    ], "only the declared counterexample trace is written"
    assert {path: after[path] for path in before if path in after} == before, (
        "and every file the model already held is unchanged and still there"
    )
    assert tree() == fixture_before, "and the tracked fixture tree is untouched"


def test_a_failed_comparison_exits_two_and_keeps_its_evidence(
    tmp_path: Path,
) -> None:
    """A doubled-bound failure is reported, recorded and leaves its traces.

    The matrix passes, so the command's own status would be ``0``; the
    comparison is what makes the bound part of the run's meaning, and a run
    that cannot compare exits ``2`` because the checker was asked and did not
    answer with a count. The artefact is written before the comparison runs, so
    this is also the regression for the file claiming a clean matrix: the
    failure record must be there, naming the configuration, the stage and the
    bound, with the counterexample and its path still readable afterwards.
    """
    model = tmp_path / "model"
    shutil.copytree(MODEL, model)
    log = tmp_path / "seen.log"
    records = tmp_path / "matrix.json"

    result = run(
        "check",
        str(model),
        "--fizz",
        str(FIZZ),
        "--fixpoint",
        "--json",
        str(records),
        "--save-traces",
        log=log,
    )

    assert result.returncode == 2, "a comparison that cannot run is not a pass"
    assert "did not pass at its doubled bound" in result.stderr, (
        "the failure names the stage that did not pass"
    )
    assert "max_actions=16" in result.stderr, "and the bound it was given"
    assert "16 doubled bound failed: counterexample for BEventually (lasso)" in (
        result.stdout
    ), "the report carries the counterexample the run found"

    written = typ.cast("list[Record]", json.loads(records.read_text(encoding="utf-8")))
    assert doubled_failure(written) == ("head", "doubled", 16, False), (
        "the failure record survived the process's non-zero exit"
    )
    failure = fixpoint_record(written)
    assert failure["verdict"] == "failed", "a failed comparison is never met"
    assert failure["property"] == "BEventually", "the counterexample is kept"
    assert failure["kind"] == "lasso", "with the shape the checker reported"
    trace = typ.cast("dict[str, object]", failure["trace"])
    assert trace["ending"] == "lasso", "and the path it walked"
    assert trace["steps"], "which is not empty"

    saved = model / "traces" / "incident-1-BEventually.json"
    assert json.loads(saved.read_text(encoding="utf-8"))["ending"] == "lasso", (
        "the matrix's own trace is still readable after the failure"
    )
    comparison = model / "traces" / "head-fixpoint-doubled.json"
    assert json.loads(comparison.read_text(encoding="utf-8"))["ending"] == "lasso", (
        "and the failed comparison's own path is saved under its stage"
    )


def test_an_unreadable_graph_reaches_the_persisted_record(tmp_path: Path) -> None:
    """A graph that cannot be read is an error in the record, with bounded evidence.

    The adapter deletes the scratch directory as the run returns, so the
    checker's transcript is the only evidence left of a run whose graph could
    not be read. The record must carry the reason and that transcript, bounded
    and with the runner's own path replaced: the artefact is committed, and a
    path that only makes sense on the machine that ran it is not evidence about
    the model. The inherited environment is checked for the same reason — the
    adapter records what the checker printed, never what it was given.
    """
    log = tmp_path / "seen.log"
    records = tmp_path / "matrix.json"
    inherited_value = "s3cr3t-do-not-record"

    result = check(
        "--json",
        str(records),
        log=log,
        MODELCHECK_E2E_ANSWER="unreadable",
        MODELCHECK_E2E_SECRET=inherited_value,
    )

    assert result.returncode == 1, "a run that proved nothing meets no expectation"
    written = typ.cast("list[Record]", json.loads(records.read_text(encoding="utf-8")))
    assert {record["verdict"] for record in written} == {"error"}, (
        "every expectation's run is recorded as an error"
    )
    detail = typ.cast("str", written[0]["detail"])
    assert "the error graph could not be read" in detail, "the reason names the read"
    assert "the checker printed" in detail, "and the transcript is kept"
    assert "reading <scratch>/model.fizz" in detail, "with the runner path replaced"
    assert ".model-check" not in detail, "and no part of it left in the record"
    assert inherited_value not in detail, "nor any inherited environment value"
    assert "first 4000 of" in detail, "the transcript is bounded and the cut stated"
    assert detail.count("x") < 5000, "so the whole runaway transcript is not kept"
    assert detail.endswith("…"), "and the omission is marked"
    assert typ.cast("float", written[0]["harness_seconds"]) >= 0.0, (
        "the harness timed the run that reported no duration of its own"
    )


def test_a_malformed_graph_reaches_the_persisted_record(tmp_path: Path) -> None:
    """A graph that is readable but is not an error graph is recorded as an error.

    The text parses as JSON, so the failure happens in the decoder rather than
    the reader. The record must name the parse and keep the document, because
    the scratch directory holding it is deleted as the run returns and a
    maintainer diagnosing the checker's output needs to see what it wrote.
    """
    log = tmp_path / "seen.log"
    records = tmp_path / "matrix.json"

    result = check("--json", str(records), log=log, MODELCHECK_E2E_ANSWER="malformed")

    assert result.returncode == 1, "a run that proved nothing meets no expectation"
    written = typ.cast("list[Record]", json.loads(records.read_text(encoding="utf-8")))
    detail = typ.cast("str", written[0]["detail"])
    assert "the error graph could not be parsed" in detail, "the reason names the parse"
    assert "the error graph held" in detail, "and the document is kept"
    assert '{"not": "a list"}' in detail, "as the checker wrote it"
    assert "the checker printed" in detail, "beside the transcript"


def test_the_stub_refuses_to_write_beside_itself(tmp_path: Path) -> None:
    """A stub run without a log path fails rather than dirtying the fixture tree.

    The stub used to default its record to a file beside itself, which made the
    documented hand-run — ``uv run python -m modelcheck check ... --fizz
    tests/fixtures/modelcheck/fizz`` — write into the tracked tree. Nothing
    caught it: ``*.log`` is git-ignored, so ``git status --porcelain``, the
    plan's own cleanliness proof, reported a clean tree while the fixture
    directory held the file. This pins the refusal, because a test that only
    checked the tree afterwards would pass again the moment the default came
    back and the leftover was cleaned up.

    ``64`` is ``EX_USAGE``: the stub was invoked in a way it cannot honour,
    which is distinct from a model that failed.
    """
    before = tree()

    result = subprocess.run(
        [str(FIZZ), "--output-dir", str(tmp_path / "out"), "model.fizz"],
        env=ambient(),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 64, "the stub refuses rather than guessing a path"
    assert "MODELCHECK_E2E_LOG" in result.stderr, "and names the variable it needs"
    assert tree() == before, "so the fixture tree is untouched"
    assert not (FIXTURES / "seen.log").exists(), "and nothing was written there"


def test_a_usage_error_names_the_problem_and_exits_two(tmp_path: Path) -> None:
    """A run the tool cannot perform is distinct from a model that failed.

    ``2`` is the status a CI job must be able to tell from ``1``: it means the
    checker was never asked, so the result says nothing about the model. The
    message goes to standard error and names the offending argument.
    """
    log = tmp_path / "seen.log"

    result = run("check", str(MODEL), log=log)

    assert result.returncode == 2, "a usage error is distinct from a failed model"
    assert not result.stdout, "the failure is reported on stderr"
    assert "modelcheck:" in result.stderr, "and the tool names itself"
    assert "--fizz" in result.stderr, "and the argument that was missing"
