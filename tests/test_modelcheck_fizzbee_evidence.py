"""The evidence a failed run keeps is read, refused and redacted deliberately.

A counterexample is only as good as the evidence beside it. The adapter reads
the checker's error graph from the directory it named, tells an absent graph
from one it could not read, and refuses a graph it cannot decode or parse
rather than guessing a trace kind from it. Each of those decisions changes what
a maintainer is told, so each is asserted here against a stub that produces the
failure rather than against a hand-built value.

The transcript and the graph are redacted on the same reasoning: the run's
scratch directory names this machine and this run and nothing about the model,
and both texts are committed. A runner path left in either would put a fact
about the host into an artefact about the model.

The stubs are written by the tests in
:mod:`tests.test_modelcheck_fizzbee_process`, which owns their helpers; this
module imports them rather than repeating them.
"""

from pathlib import Path

import pytest
from test_modelcheck_fizzbee_process import _spec, _stub

from modelcheck import domain
from modelcheck.adapters.fizzbee_process import FizzbeeChecker

_FAILED = (
    "printf 'Valid Nodes: 4 Unique states: 4\\nFAILED: Liveness check failed\\n'\n"
)
"""The output a stub prints after it has written its error graph."""


def _graph_run(tmp_path: Path, prepare: str) -> domain.CheckResult:
    """Run a stub that prepares an error graph and then reports a failure.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The scratch directory the stub is written into.
    prepare : str
        The shell lines that write ``out/error-graph.json``. Each test supplies
        the one failure it is about, so the difference between the tests is the
        graph rather than the surrounding run.

    Returns
    -------
    domain.CheckResult
        What the adapter made of the run.
    """
    _stub(tmp_path, "#!/usr/bin/env bash\n" + prepare + _FAILED)
    return FizzbeeChecker(tmp_path / "fizz").check(_spec())


def test_an_error_graph_is_read_from_the_named_output_directory() -> None:
    """A graph that is not there is absent, not an exception.

    A run that passed, or a counterexample kind that carries no graph, writes no
    file. Absence is ordinary, so it must not become an error that hides the
    verdict the output already gave.
    """
    from modelcheck.adapters.fizzbee_process import _graph

    assert _graph(Path("/nonexistent")) is None, "a missing graph is absent, not raised"


def test_an_unreadable_error_graph_is_reported_rather_than_absent(
    tmp_path: Path,
) -> None:
    """A graph that exists but cannot be read is an error, not a missing graph.

    Only absence is ordinary: a permission or I/O failure means the evidence the
    counterexample needs is missing for a reason the run report must name.
    Reporting it as absent would turn a broken read into a pass, or into a
    finding it cannot support.
    """
    from modelcheck.adapters.fizzbee_process import _graph

    (tmp_path / "error-graph.json").mkdir()

    with pytest.raises(IsADirectoryError):
        _graph(tmp_path)


def test_a_run_whose_graph_cannot_be_read_is_a_run_that_proved_nothing(
    tmp_path: Path,
) -> None:
    """An unreadable graph is reported as an error, not as a counterexample.

    The adapter is the only place that can tell absence from a failed read, and
    the difference decides whether a violation is reported with evidence or
    with a trace kind nobody established. A directory where the graph should be
    is the cheapest unreadable graph to produce.
    """
    result = _graph_run(tmp_path, "mkdir -p out/error-graph.json\n")

    assert isinstance(result, domain.Errored), "the run proved nothing"
    assert "could not be read" in result.detail, "and the reason names the read"
    assert "the checker printed:\nValid Nodes: 4 Unique states: 4\n" in result.detail, (
        "the transcript is kept because the scratch directory holding it is deleted"
    )


@pytest.mark.parametrize(
    ("prepare", "reason"),
    [
        (
            "mkdir -p out\nprintf '\\xff\\xfe' > out/error-graph.json\n",
            "could not be read",
        ),
        (
            "mkdir -p out\nprintf '{not json' > out/error-graph.json\n",
            "could not be parsed",
        ),
    ],
    ids=["undecodable", "unparsable"],
)
def test_a_graph_the_adapter_cannot_turn_into_a_trace_proved_nothing(
    tmp_path: Path, prepare: str, reason: str
) -> None:
    """A graph that cannot become a trace is an error, not a missing graph.

    Two stages can refuse it, and both must refuse at the same boundary. Bytes
    nobody can decode fail while the file is read, arriving as a
    ``UnicodeDecodeError`` rather than an ``OSError``. Text that decodes but
    parses as nothing fails in the parser instead. Either way the violation's
    path cannot be read, so the run proved nothing and no shape-specific
    expectation may be met by it; letting either escape as a traceback would
    turn a broken graph into a crash rather than a report.
    """
    result = _graph_run(tmp_path, prepare)

    assert isinstance(result, domain.Errored), "the run proved nothing"
    assert reason in result.detail, f"and the reason names the stage: {reason}"


def test_a_scratch_path_in_the_error_graph_is_redacted(tmp_path: Path) -> None:
    """A graph kept as evidence is redacted like the transcript beside it.

    The graph is checker-written text about the model, but it is written into
    the run's scratch directory and can name that directory. Discarded as
    evidence it would put a runner path into the committed record exactly as a
    printed one would, so the adapter replaces it before the text leaves.
    """
    result = _graph_run(
        tmp_path,
        'mkdir -p out\nprintf \'{"state": "%s"}\' "$PWD" > out/error-graph.json\n',
    )

    assert isinstance(result, domain.Errored), "a mapping is not an error graph"
    assert "the error graph could not be parsed" in result.detail, (
        "the refusal names the parse"
    )
    assert '{"state": "<scratch>"}' in result.detail, "and the runner path is replaced"
    assert ".model-check" not in result.detail, "with no part of it left behind"


def test_a_scratch_path_the_checker_printed_is_redacted(tmp_path: Path) -> None:
    """A runner path is not evidence about the model, so it is replaced.

    The scratch directory's absolute path identifies this machine and this run
    and nothing about the model. A record that carried it would put a runner
    path into an artefact a maintainer commits, so the adapter replaces it
    before the text becomes a diagnostic.
    """
    fizz = _stub(
        tmp_path,
        "#!/usr/bin/env bash\nprintf 'reading %s/model.fizz\\n' \"$PWD\"\n",
    )

    result = FizzbeeChecker(fizz).check(_spec())

    assert isinstance(result, domain.Errored), "no verdict line is an error"
    assert "<scratch>/model.fizz" in result.detail, "the path is replaced"
    assert ".model-check" not in result.detail, "and the runner path is gone"
