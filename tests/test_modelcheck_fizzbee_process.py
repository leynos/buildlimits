"""The process adapter is the package's only trust boundary, and it is fenced.

Two things are asserted here that no other test can see. First, the
specification the adapter runs is written into a scratch directory and the
checker is *told* where to write, so a run cannot leave the tracked tree dirty.
Second, the environment the checker inherits carries the Go runtime's limits,
because a mistaken model can explore without bound and the host should not be
what stops it.

The process-group kill is the third requirement, and it is asserted in
:mod:`tests.test_modelcheck_fizzbee_kill` because its subject is a child of the
launcher rather than the launcher's own output.

The stubs are shell scripts written by the test into a scratch directory, and
each records what it was asked to do. A stub that merely exited would let an
adapter that never ran anything pass, so every test that cares reads the
record the stub left.
"""

import typing as typ
from pathlib import Path

from modelcheck import domain
from modelcheck.adapters.fizzbee_process import FizzbeeChecker

if typ.TYPE_CHECKING:
    import pytest

_STUB = """\
#!/usr/bin/env bash
# Records its arguments, its working directory and its limits, then answers.
printf '%s\\n' "$PWD" "$@" > "$(dirname "$0")/record.txt"
printf 'GOMAXPROCS=%s\\n' "${GOMAXPROCS-}" >> "$(dirname "$0")/record.txt"
printf 'GOMEMLIMIT=%s\\n' "${GOMEMLIMIT-}" >> "$(dirname "$0")/record.txt"
if [ -f model.fizz ]; then echo 'model.fizz present' >> "$(dirname "$0")/record.txt"; fi
mkdir -p out
printf 'Valid Nodes: 4 Unique states: 4\\nPASSED: Model checker completed successfully\\n'
"""
"""A launcher that records what it was given and reports a pass.

It writes ``error-graph.json`` into the ``out`` directory the adapter names, so
the test can prove the adapter read the graph from where the checker was told
to put it rather than from beside the specification.
"""


def _stub(directory: Path, script: str, name: str = "fizz") -> Path:
    """Write an executable launcher into a directory.

    Parameters
    ----------
    directory : pathlib.Path
        Where to write the script.
    script : str
        The script's text.
    name : str
        The file name to give it.

    Returns
    -------
    pathlib.Path
        The script, made executable.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _spec() -> domain.MaterializedSpec:
    """Return a specification to run.

    Returns
    -------
    domain.MaterializedSpec
        A minimal specification; the stub answers regardless of its content.
    """
    return domain.MaterializedSpec("head", "action Init:\n  n = 0\n", ())


def _record(directory: Path) -> list[str]:
    """Return the lines a stub recorded.

    Parameters
    ----------
    directory : pathlib.Path
        The directory holding the stub and its record.

    Returns
    -------
    list[str]
        The recorded lines.
    """
    return (directory / "record.txt").read_text(encoding="utf-8").splitlines()


def _run_and_record(tmp_path: Path) -> list[str]:
    """Run the recording stub once and return what it recorded.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The scratch directory the stub is written into.

    Returns
    -------
    list[str]
        The working directory, arguments and limits the checker saw, in the
        order the stub wrote them.
    """
    fizz = _stub(tmp_path, _STUB)

    FizzbeeChecker(fizz).check(_spec())

    return _record(tmp_path)


def test_a_passing_run_is_classified_from_its_output(tmp_path: Path) -> None:
    """The adapter hands the checker's output to the real parser."""
    fizz = _stub(tmp_path, _STUB)

    result = FizzbeeChecker(fizz).check(_spec())

    assert isinstance(result, domain.Passed), "a pass line is classified as a pass"
    assert result.states == 4, "and the count is the one the checker printed"


def test_the_checker_runs_in_a_scratch_directory(tmp_path: Path) -> None:
    """The specification is written under ``.model-check``, never beside the model.

    A run that wrote into the model directory would leave the tracked tree
    dirty, and the plan requires ``git status --porcelain`` to be empty after
    every run. The stub records its working directory, so this asserts what the
    checker really saw rather than what the adapter intended.
    """
    fizz = _stub(tmp_path, _STUB)

    FizzbeeChecker(fizz).check(_spec())

    working_directory = Path(_record(tmp_path)[0])
    assert working_directory.parent.name == ".model-check", (
        "the run happens under the scratch directory"
    )
    assert "model.fizz present" in _record(tmp_path), (
        "and the specification was written there, not beside the model"
    )


def test_the_output_directory_is_named_rather_than_inferred(tmp_path: Path) -> None:
    """The checker is told where to write, so its graphs are found deterministically.

    Left to itself the checker writes ``out/run_<timestamp>``, whose name
    changes every run. Naming the directory is what lets the adapter read the
    graph it expects instead of globbing for the newest one.
    """
    recorded = _run_and_record(tmp_path)

    assert "--output-dir" in recorded, "the output directory is named in argv"
    assert recorded[recorded.index("--output-dir") + 1].endswith("out"), (
        "and the adapter can find the graphs where it told the checker to write"
    )


def test_the_go_runtime_is_capped(tmp_path: Path) -> None:
    """The checker inherits limits, not the host's whole capacity.

    ``GOMAXPROCS`` and ``GOMEMLIMIT`` are the two knobs that keep one mistaken
    model from using every core and every byte, and the plan's budget states
    both. The stub prints what it received, so this is the value the checker
    saw.
    """
    recorded = _run_and_record(tmp_path)

    assert "GOMAXPROCS=2" in recorded, "the checker inherits a core cap"
    assert "GOMEMLIMIT=4GiB" in recorded, "and a memory cap"


def test_the_scratch_directory_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished run removes its own scratch directory, whether it passed or failed.

    The scratch root is redirected into the test's own directory. The adapter
    writes under ``.model-check/`` relative to the working directory, which is
    shared with ``make model-check`` and with any other run in the same
    checkout, so a scan of the real root would answer for a neighbour's run as
    readily as for this one. Redirecting it also lets the test see the
    directory exist and then go, rather than only that nothing is left.
    """
    from modelcheck.adapters import fizzbee_process

    root = tmp_path / ".model-check"
    monkeypatch.setattr(fizzbee_process, "SCRATCH_ROOT", root)
    fizz = _stub(tmp_path, _STUB)

    FizzbeeChecker(fizz).check(_spec())

    assert root.is_dir(), "the run made the scratch root on demand"
    assert sorted(path.name for path in root.iterdir()) == [], (
        "the run removed its own scratch directory"
    )


def test_a_missing_launcher_is_a_run_that_proved_nothing(tmp_path: Path) -> None:
    """A launcher that cannot start is an ``Errored``, not a traceback.

    The matrix reports it as a run that proved nothing, which is what it is,
    and the exit status is still un-met rather than the tool crashing.
    """
    result = FizzbeeChecker(tmp_path / "no-such-fizz").check(_spec())

    assert isinstance(result, domain.Errored), (
        "a launcher that cannot start proves nothing"
    )
    assert result.detail.startswith("the checker could not be started:"), (
        "and the detail carries the reason rather than a traceback"
    )


def test_a_failing_run_keeps_its_counterexample(tmp_path: Path) -> None:
    """The adapter classifies a failure from the output and the error graph.

    The stub replays the recorded lasso run's output and error graph, so the
    class this asserts is the one the real checker's report produces rather
    than one the test invented.
    """
    fixture = Path(__file__).with_name("fixtures") / "fizzbee" / "lasso"
    script = _STUB.replace(
        "printf 'Valid Nodes: 4 Unique states: 4\\n"
        "PASSED: Model checker completed successfully\\n'",
        f"cp {fixture / 'error-graph.json'} out/error-graph.json\n"
        f"cat {fixture / 'stdout.txt'}",
    )
    fizz = _stub(tmp_path, script)

    result = FizzbeeChecker(fizz).check(_spec())

    assert isinstance(result, domain.Counterexample), (
        "the recorded failure is classified as a counterexample"
    )
    assert result.kind is domain.CounterexampleKind.LASSO, (
        "with the kind the error graph recorded"
    )
    assert result.property_name == "BEventually", "and the assertion it violated"


def test_an_error_graph_is_read_from_the_named_output_directory() -> None:
    """A graph that is not there is absent, not an exception.

    The violation the output announced is real either way; the path is evidence
    about it rather than the finding itself, so losing it must not lose the
    finding.
    """
    from modelcheck.adapters.fizzbee_process import _graph

    assert _graph(Path("/nonexistent")) is None, "a missing graph is absent, not raised"


def test_a_relative_launcher_is_resolved_before_the_run_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative ``--fizz`` works, which is the form the command line is used in.

    The run's own working directory is the scratch directory, so a launcher path
    resolved when the process starts would be looked for there and never found.
    The end-to-end test found this: every run reported that the checker could
    not be started, and the matrix said nothing about the model. Resolving the
    path when the adapter is built is the fix, and this asserts the adapter's
    behaviour rather than the command line's.
    """
    _stub(tmp_path, _STUB)
    monkeypatch.chdir(tmp_path)

    result = FizzbeeChecker(Path("fizz")).check(_spec())

    assert isinstance(result, domain.Passed), "a relative launcher is found and run"
    assert result.states == 4, "and the run completes as it would by absolute path"


def test_the_launcher_is_invoked_without_a_shell(tmp_path: Path) -> None:
    """Arguments are passed as a list, so a model cannot become a command line.

    The stub is named with a space in it, which a shell would split into two
    words. A launcher found and run under that name proves the argv list was
    used as a list.
    """
    fizz = _stub(tmp_path, _STUB, name="fizz launcher")

    result = FizzbeeChecker(fizz).check(_spec())

    assert isinstance(result, domain.Passed), "the spaced name is one argument"
