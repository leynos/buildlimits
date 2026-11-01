"""The command line as the maintainer runs it, with a stub in place of the binary.

Every other command-line test builds the app in-process and hands it doubles.
That leaves three things unproven, and this module is here for exactly those:

- ``python -m modelcheck`` starts at all, which means the real adapter wiring in
  :mod:`modelcheck.__main__` is exercised rather than bypassed;
- a *relative* ``--fizz`` path works. The adapter runs the checker inside a
  scratch directory, so a launcher path resolved at run time would be looked
  for there; the end-to-end run is what found that, and it is why the path is
  resolved when the adapter is built;
- the run leaves nothing in the tracked tree. ``.model-check/`` is git-ignored
  and each run's directory is removed, so ``git status --porcelain`` is still
  empty afterwards — the plan's constraint on every checker run.

The stub is a shell script in the fixture directory. It writes nothing there:
its record of what it was asked goes to the path ``MODELCHECK_E2E_LOG`` names,
which is a scratch directory the test owns. That is what lets the scratch-path
assertion be made at all without the test itself dirtying the tree it is
guarding.

``uv run`` is used rather than the current interpreter because it is the way a
maintainer invokes the tool, and because it is the invocation the plan names.
"""

import json
import os
import subprocess
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

_ROOT = Path(__file__).resolve().parents[1]
"""The checkout root, which is where ``uv run`` finds the project."""

_FIXTURES = Path(__file__).with_name("fixtures") / "modelcheck"
"""The fixture directory holding the model, its configurations and the stub."""

_MODEL = _FIXTURES / "model"
_FIZZ = _FIXTURES / "fizz"

_TIMEOUT_SECONDS = 120
"""Enough for ``uv`` to resolve the project and the run to finish twice over.

The matrix itself is three stub invocations of a few milliseconds. The bound is
generous because the first ``uv run`` in a session may have to check the lock
file, and a flaky bound would be blamed on the code under test.
"""


def _ambient() -> dict[str, str]:
    """Return the caller's environment with every ``MODELCHECK_*`` removed.

    Stripping the whole prefix rather than only the names this module passes is
    what stops an ambient ``MODELCHECK_FIZZ`` or ``MODELCHECK_BOUNDS`` from
    changing what a test measures. Both helpers start here, so a variable added
    to one run's environment cannot leak into the other's by an oversight.

    Returns
    -------
    dict[str, str]
        The caller's environment, without any ``MODELCHECK_*`` setting.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MODELCHECK_")
    }


def _environment(log: Path, **overrides: str) -> dict[str, str]:
    """Return the environment one command-line run gets.

    Parameters
    ----------
    log : pathlib.Path
        Where the stub is to record each invocation.
    **overrides : str
        Variables to set or replace for this run.

    Returns
    -------
    dict[str, str]
        The ambient environment, plus the stub's log and the caller's
        overrides.
    """
    environment = _ambient()
    environment["MODELCHECK_E2E_LOG"] = str(log)
    environment.update(overrides)
    return environment


def _run(
    *arguments: str, log: Path, **overrides: str
) -> subprocess.CompletedProcess[str]:
    """Run the command line as a maintainer would.

    Parameters
    ----------
    *arguments : str
        The arguments after ``python -m modelcheck``.
    log : pathlib.Path
        Where the stub is to record each invocation.
    **overrides : str
        Variables to set or replace for this run.

    Returns
    -------
    subprocess.CompletedProcess
        The finished run, with its output decoded as text.
    """
    return subprocess.run(
        ["uv", "run", "python", "-m", "modelcheck", *arguments],
        cwd=_ROOT,
        env=_environment(log, **overrides),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


def _check(
    *extra: str, log: Path, **overrides: str
) -> subprocess.CompletedProcess[str]:
    """Run ``modelcheck check`` over the fixture model.

    Parameters
    ----------
    *extra : str
        Further arguments to the ``check`` command.
    log : pathlib.Path
        Where the stub is to record each invocation.
    **overrides : str
        Variables to set or replace for this run.

    Returns
    -------
    subprocess.CompletedProcess
        The finished run.
    """
    return _run(
        "check", str(_MODEL), "--fizz", str(_FIZZ), *extra, log=log, **overrides
    )


def _invocations(log: Path) -> list[tuple[str, str]]:
    """Return what the stub was asked to do, one pair per run.

    Parameters
    ----------
    log : pathlib.Path
        The file the stub appended to.

    Returns
    -------
    list[tuple[str, str]]
        Each run's working directory and the arguments it was given.
    """
    lines = log.read_text(encoding="utf-8").splitlines()
    return list(zip(lines[0::2], lines[1::2], strict=True))


def _tree() -> list[Path]:
    """Return every path under the fixture directory.

    Returns
    -------
    list[Path]
        The fixture tree's paths, relative to it and sorted, so two snapshots
        compare as sets of names rather than as directory listings.
    """
    return sorted(path.relative_to(_FIXTURES) for path in _FIXTURES.rglob("*"))


def test_a_met_matrix_exits_zero(tmp_path: Path) -> None:
    """The whole matrix, run as a command, reports every expectation met.

    The two configurations are answered differently by the stub: the healthy
    one holds, and the incident is still reproduced. A run that reported only
    the first would pass this matrix whether or not the model still found the
    incident, so the second line is the one that makes the exit status mean
    something.
    """
    log = tmp_path / "seen.log"

    result = _check(log=log)

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

    _check(log=log)

    invocations = _invocations(log)
    assert len(invocations) == 2, "one run per configuration"
    for directory, arguments in invocations:
        working_directory = Path(directory)
        assert working_directory.parent.name == ".model-check", (
            "the checker runs under the scratch directory"
        )
        assert not working_directory.is_relative_to(_MODEL), (
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

    result = _check("--json", str(records), log=log)

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

    result = _check(log=log, MODELCHECK_E2E_ANSWER="pass")

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
    before = _tree()

    _check("--json", str(tmp_path / "matrix.json"), log=log)
    _check(log=log, MODELCHECK_E2E_ANSWER="pass")

    assert _tree() == before, "the fixture tree holds the same paths afterwards"
    assert not (_FIXTURES / "seen.log").exists(), (
        "and the stub's record went to the scratch directory it was given"
    )


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
    before = _tree()

    result = subprocess.run(
        [str(_FIZZ), "--output-dir", str(tmp_path / "out"), "model.fizz"],
        env=_ambient(),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 64, "the stub refuses rather than guessing a path"
    assert "MODELCHECK_E2E_LOG" in result.stderr, "and names the variable it needs"
    assert _tree() == before, "so the fixture tree is untouched"
    assert not (_FIXTURES / "seen.log").exists(), "and nothing was written there"


def test_a_usage_error_names_the_problem_and_exits_two(tmp_path: Path) -> None:
    """A run the tool cannot perform is distinct from a model that failed.

    ``2`` is the status a CI job must be able to tell from ``1``: it means the
    checker was never asked, so the result says nothing about the model. The
    message goes to standard error and names the offending argument.
    """
    log = tmp_path / "seen.log"

    result = _run("check", str(_MODEL), log=log)

    assert result.returncode == 2, "a usage error is distinct from a failed model"
    assert not result.stdout, "the failure is reported on stderr"
    assert "modelcheck:" in result.stderr, "and the tool names itself"
    assert "--fizz" in result.stderr, "and the argument that was missing"
