"""The runs and trace checks the redaction tests share.

Each redaction test needs a checker run that ends a particular way: an error
whose transcript is the subject, an error the adapter refused after reading a
graph, or a lasso decoded from a graph the test rewrote. Producing those runs is
plumbing rather than the behaviour a test states, so it lives here. The trace
check is here for the same reason: every lasso test makes the same two claims
about it.

This module is test support and is linted as production code, following the
convention in ``docs/repository-layout.md``: ``test_*.py`` modules hold tests,
anything else here holds helpers. A precondition is therefore failed through
:func:`pytest.fail` rather than an ``assert`` statement, which the lint rules
reserve for test modules.
"""

import typing as typ

import pytest
from modelcheck_evidence_cases import LASSO_GRAPH, graph_run, run_stub

from modelcheck import domain

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

PLACEHOLDER = "<redacted>"
"""What every redaction test expects to see in place of a credential."""


def _require(*, condition: bool, message: str) -> None:
    """Fail the test when ``condition`` is false.

    Parameters
    ----------
    condition : bool
        The precondition the run was expected to meet.
    message : str
        What was expected, which becomes the failure message.
    """
    if not condition:
        pytest.fail(message)


def _errored(result: domain.CheckResult, message: str) -> domain.Errored:
    """Return ``result`` as an error, failing the test when it is not one.

    Parameters
    ----------
    result : domain.CheckResult
        What the adapter made of the run.
    message : str
        What the run was expected to be, used as the failure message.

    Returns
    -------
    domain.Errored
        The same result, narrowed to the error it was required to be.
    """
    _require(condition=isinstance(result, domain.Errored), message=message)
    return typ.cast("domain.Errored", result)


def errored_run(
    tmp_path: Path,
    script: str,
    environ: cabc.Mapping[str, str] | None = None,
) -> domain.Errored:
    """Run a stub that prints no verdict and return the error it produced.

    A stub that prints no verdict line is the error every transcript test is
    about, so the check that the run ended that way is a precondition here
    rather than an assertion each test would repeat.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The scratch directory the stub is written into.
    script : str
        The stub's shell script.
    environ : collections.abc.Mapping[str, str] | None
        Values added to the environment the checker runs with.

    Returns
    -------
    domain.Errored
        The result, whose detail is the transcript.
    """
    return _errored(
        run_stub(tmp_path, script, environ), "a run without a verdict is an error"
    )


def stub_graph_run(
    tmp_path: Path,
    graph_text: str,
    environ: cabc.Mapping[str, str] | None = None,
) -> domain.CheckResult:
    """Run a stub over ``graph_text`` as the error graph it writes.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The scratch directory the stub is written into.
    graph_text : str
        The exact text to write as ``out/error-graph.json``.
    environ : collections.abc.Mapping[str, str] | None
        Values added to the environment the checker runs with.

    Returns
    -------
    domain.CheckResult
        What the adapter made of the run.
    """
    graph = tmp_path / "graph.json"
    graph.write_text(graph_text, encoding="utf-8")
    return graph_run(
        tmp_path, f'mkdir -p out\ncp "{graph}" out/error-graph.json\n', environ
    )


def errored_graph(
    tmp_path: Path,
    graph_text: str,
    environ: cabc.Mapping[str, str] | None = None,
) -> domain.Errored:
    """Run a stub over ``graph_text`` and return the error it produced.

    A graph the adapter refuses is the subject of each test that uses this, so
    the check that the run ended that way is a precondition here rather than an
    assertion each test would repeat.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The scratch directory the stub is written into.
    graph_text : str
        The exact text to write as ``out/error-graph.json``.
    environ : collections.abc.Mapping[str, str] | None
        Values added to the environment the checker runs with.

    Returns
    -------
    domain.Errored
        The result, whose detail quotes the refused graph.
    """
    return _errored(
        stub_graph_run(tmp_path, graph_text, environ), "a refused graph is an error"
    )


def lasso_run(
    tmp_path: Path,
    edit: cabc.Callable[[str], str],
    environ: cabc.Mapping[str, str] | None = None,
) -> domain.Counterexample:
    """Run a stub over a recorded lasso that ``edit`` has rewritten.

    The recorded lasso is the one graph that parses into a trace, so a test
    about classification changes the text and leaves the shape alone.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The scratch directory the stub is written into.
    edit : collections.abc.Callable[[str], str]
        Rewrites the recorded graph's text before it is written.
    environ : collections.abc.Mapping[str, str] | None
        Values added to the environment the checker runs with.

    Returns
    -------
    domain.Counterexample
        The classified lasso, with its trace.
    """
    result = stub_graph_run(
        tmp_path, edit(LASSO_GRAPH.read_text(encoding="utf-8")), environ
    )
    _require(
        condition=isinstance(result, domain.Counterexample),
        message=getattr(result, "detail", "the graph did not decode into a lasso"),
    )
    return typ.cast("domain.Counterexample", result)


def assert_detail_replaces(detail: str, *, kept: str, gone: str) -> None:
    """Fail the test unless a transcript keeps ``kept`` and drops ``gone``.

    Every transcript redaction test makes the same two claims: the part of the
    line that says what the checker printed survives, and the value identifying
    the runner does not. Stating them once keeps each test to the stub that
    produced the text and the two fragments it is about.

    Parameters
    ----------
    detail : str
        The transcript the adapter kept for the run.
    kept : str
        The fragment the record must still carry.
    gone : str
        The fragment the record must not carry.
    """
    _require(condition=kept in detail, message=f"the detail keeps {kept!r}")
    _require(condition=gone not in detail, message=f"and does not keep {gone!r}")


def assert_trace_hides(result: domain.Counterexample, *credentials: str) -> None:
    """Fail the test when the trace carries no placeholder or a credential.

    Parameters
    ----------
    result : domain.Counterexample
        A classified lasso whose trace has been redacted.
    *credentials : str
        The values the run was given, none of which may reach the trace.
    """
    recorded = [str(state) for state in result.trace.states]
    _require(
        condition=any(PLACEHOLDER in state for state in recorded),
        message="the decoded value is replaced",
    )
    _require(
        condition=not any(
            value in state for state in recorded for value in credentials
        ),
        message="and no credential reaches the trace",
    )
