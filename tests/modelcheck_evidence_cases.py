"""The run the evidence tests repeat, shared by the modules that need it.

:mod:`tests.test_modelcheck_fizzbee_evidence` and
:mod:`tests.test_modelcheck_fizzbee_redaction` both start a stub checker and
assert on what the adapter made of it. What differs between their tests is the
graph or the transcript, and that stays in the test that is about it; the run
around it, and the recorded lasso some of them need, are the same everywhere and
live here rather than being copied into both modules.

This is a helper module rather than a test module, so nothing in it is
collected as a test of its own.
"""

import os
import typing as typ
from pathlib import Path
from unittest import mock

from fizzbee_process_stubs import spec, stub

from modelcheck.adapters.fizzbee_process import FizzbeeChecker

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck import domain

FAILED_OUTPUT = (
    "printf 'Valid Nodes: 4 Unique states: 4\\nFAILED: Liveness check failed\\n'\n"
)
"""The output a stub prints after it has written its error graph."""

LASSO_GRAPH = (
    Path(__file__).with_name("fixtures") / "modelcheck" / "lasso-error-graph.json"
)
"""A recorded lasso the tests use when the graph must parse into a trace."""


def run_stub(
    tmp_path: Path,
    script: str,
    environ: cabc.Mapping[str, str] | None = None,
) -> domain.CheckResult:
    """Run one stub checker under the environment the test needs.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The scratch directory the stub is written into.
    script : str
        The stub's shell body: what the checker prints and writes.
    environ : collections.abc.Mapping[str, str] | None
        Values added to ``os.environ`` for the duration of the run, and so to
        the values the checker inherits and can echo.

    Returns
    -------
    domain.CheckResult
        What the adapter made of the run, unclassified by this helper.
    """
    fizz = stub(tmp_path, script)
    with mock.patch.dict(os.environ, environ or {}):
        return FizzbeeChecker(fizz).check(spec())


def graph_run(
    tmp_path: Path,
    prepare: str,
    environ: cabc.Mapping[str, str] | None = None,
) -> domain.CheckResult:
    """Run a stub that prepares an error graph and then reports a failure.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The scratch directory the stub is written into.
    prepare : str
        The shell lines that write ``out/error-graph.json``. Each test supplies
        the one failure it is about, so the difference between the tests is the
        graph rather than the surrounding run.
    environ : collections.abc.Mapping[str, str] | None
        Values added to the environment the checker runs with.

    Returns
    -------
    domain.CheckResult
        What the adapter made of the run.
    """
    return run_stub(
        tmp_path, "#!/usr/bin/env bash\n" + prepare + FAILED_OUTPUT, environ
    )
