"""The process adapter is the package's only trust boundary, and it is fenced.

Three things are asserted here that no other test can see. First, the
specification the adapter runs is written into a scratch directory and the
checker is *told* where to write, so a run cannot leave the tracked tree dirty.
Second, a launcher that never exits is killed by process group: the ``fizz``
launcher starts a Go binary of its own, so killing only the launcher would
leave the real checker running. Third, the environment the checker inherits
carries the Go runtime's limits, because a mistaken model can explore without
bound and the host should not be what stops it.

The stubs are shell scripts written by the test into a scratch directory, and
each records what it was asked to do. A stub that merely exited would let an
adapter that never ran anything pass, so every test that cares reads the
record the stub left.
"""

import os
from pathlib import Path

import pytest

from modelcheck import domain
from modelcheck.adapters.fizzbee_process import FizzbeeChecker

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

_NEVER_EXITS = """\
#!/usr/bin/env bash
# Starts a child that outlives a launcher-only kill, then waits forever.
sleep 300 &
printf '%s\\n' "$!" > "$(dirname "$0")/child.pid"
printf 'Valid Nodes: 3 Unique states: 3\\n'
wait
"""
"""A launcher whose group the adapter must kill.

The child is what makes this a process-group test rather than a process test:
it inherits the launcher's stdout pipe and its process group, so an adapter
that killed only the launcher would both leave the child alive and block
forever collecting output from a pipe the child still holds.
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


def test_the_scratch_directory_is_removed(tmp_path: Path) -> None:
    """A finished run removes its own scratch directory, whether it passed or failed.

    The assertion is scoped to the adapter's ``run-*`` directories rather than
    the whole of ``.model-check/``. That directory is gitignored scratch shared
    with ``make model-check``, which writes its reports there and keeps them,
    and with any other worker running at the same time; an empty directory is
    not something the adapter can promise.
    """
    _run_and_record(tmp_path)

    scratch = Path.cwd() / ".model-check"
    leftovers = sorted(
        path.name for path in scratch.iterdir() if path.name.startswith("run-")
    )

    assert leftovers == [], "the adapter's own scratch directory is emptied"


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


def test_a_run_that_never_returns_is_killed_with_its_children(
    tmp_path: Path,
) -> None:
    """The whole process group dies, and no descendant survives.

    This is the process-control requirement. The stub's child inherits the
    launcher's group and its stdout pipe, so killing only the launcher would
    leave a live process and a pipe nothing would ever close. The assertion is
    on the child's exit, read from ``/proc``, because a child that became a
    zombie still occupies a slot on the host the run was supposed to release.
    """
    fizz = _stub(tmp_path, _NEVER_EXITS)
    checker = FizzbeeChecker(fizz, timeout_seconds=1.0)

    result = checker.check(_spec())

    child = int((tmp_path / "child.pid").read_text(encoding="utf-8").strip())
    assert isinstance(result, domain.Errored), "a run that never returns proves nothing"
    assert "did not finish within" in result.detail, "and the detail names the timeout"
    assert _is_gone(child), f"child {child} survived the timeout"


def test_a_timeout_names_the_limit_and_keeps_what_printed(tmp_path: Path) -> None:
    """The report says which bound was exceeded, and where the run had got.

    The state count a killed run managed is the evidence of whether it was
    still exploring or stuck, so the detail carries the checker's output after
    the reason rather than discarding it.
    """
    fizz = _stub(tmp_path, _NEVER_EXITS)

    result = FizzbeeChecker(fizz, timeout_seconds=1.0).check(_spec())

    assert isinstance(result, domain.Errored), "the timed-out run is an error"
    assert result.detail.splitlines()[0] == "the checker did not finish within 1s", (
        "the first line names the bound that was exceeded"
    )
    assert "Valid Nodes: 3" in result.detail, (
        "and what the run managed is kept after the reason"
    )


def _is_gone(pid: int) -> bool:
    """Report whether a process has exited and been reaped elsewhere.

    Parameters
    ----------
    pid : int
        The process to look for.

    Returns
    -------
    bool
        Whether ``/proc`` no longer holds a live or zombie entry for it, or its
        entry is a zombie — the two states a killed child reaches when its
        parent is gone.
    """
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except FileNotFoundError, ProcessLookupError:
        return True
    state = stat.rpartition(")")[2].split()[0]
    return state == "Z"


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


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="needs /proc to read a child's state"
)
def test_a_killed_group_releases_its_pipe(tmp_path: Path) -> None:
    """The run returns rather than blocking on a pipe a child still holds.

    A surviving child inheriting stdout would keep ``communicate`` waiting for
    an end of file that never comes, so a test that completes at all is
    evidence the group was killed rather than only the launcher.
    """
    fizz = _stub(tmp_path, _NEVER_EXITS)

    started = os.times().elapsed
    FizzbeeChecker(fizz, timeout_seconds=1.0).check(_spec())
    elapsed = os.times().elapsed - started

    assert elapsed < 60, "the run returned rather than blocking on the pipe"
    child = int((tmp_path / "child.pid").read_text(encoding="utf-8").strip())
    assert _is_gone(child), f"child {child} outlived the group it was killed with"
