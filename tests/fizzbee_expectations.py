"""State what a FizzBee run must show, and enforce it with one call.

Every model question starts the same way: the spec has to compile (a compile
failure exits 1 and writes no run directory), the run has to reach the verdict
the question is about, and a question about the explored state space has to
see the counts it predicts. Writing those checks in each test would bury the
property under boilerplate, and the repository's duplication gate would see
the same statements repeated in every module.

Two shapes cover every test. :func:`check` runs one model against one
:class:`Expectation`. :func:`paired` runs a model that must pass beside the
near-identical model that must fail, which is how the fairness and
interleaving questions are posed.

The checks raise through ``pytest.fail`` rather than ``assert`` so that a
failure names the model and the expectation even when the test runs with
assertions rewritten, and so that this support module needs no lint
suppression for ``assert``.
"""

import typing as typ

import pytest
from fizzbee_spike import (
    CheckerRun,
    error_graph,
    link_names,
    run_model,
    state_counts,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

PASSED = "PASSED: Model checker completed successfully"
"""The line the checker prints when a model passes."""

FAILED = "FAILED: Model checker failed"
"""The prefix of the safety and deadlock failure lines."""

FAILED_LIVENESS = "FAILED: Liveness check failed"
"""The line a liveness check prints when it finds a counterexample."""


class Expectation(typ.NamedTuple):
    """A model and what its run must show.

    Attributes
    ----------
    model : str
        File name under ``models/spike``.
    verdict : str
        A line fragment the output must contain, such as :data:`PASSED`.
    why : str
        The expectation in words, used in every failure message.
    counts : tuple[int, int, int] | None
        The expected node, valid-node and unique-state counts, or ``None``
        when the question is not about the size of the explored space.
    options : tuple[str, ...]
        Extra launcher flags, such as ``--no-symmetry-reduction``.
    trace : tuple[str, ...]
        The action names the counterexample must step through, in order, or
        empty when the question does not turn on the trace.

    Examples
    --------
    >>> Expectation("deadlock.fizz", FAILED, "no step can run").verdict
    'FAILED: Model checker failed'
    >>> Expectation("symmetry.fizz", PASSED, "it passes", (9, 7, 3)).counts
    (9, 7, 3)
    >>> Expectation("w.fizz", FAILED, "starved", trace=("Init", "stutter")).trace
    ('Init', 'stutter')
    """

    model: str
    verdict: str
    why: str
    counts: tuple[int, int, int] | None = None
    options: tuple[str, ...] = ()
    trace: tuple[str, ...] = ()


def check(directory: Path, expectation: Expectation) -> CheckerRun:
    """Run one model and fail the test unless the run met *expectation*.

    Parameters
    ----------
    directory : Path
        A directory owned by this test.
    expectation : Expectation
        The model, verdict and counts the run must show.

    Returns
    -------
    CheckerRun
        The run, ready to have its graphs read.

    Examples
    --------
    >>> from pathlib import Path
    >>> want = Expectation("m.fizz", PASSED, "it passes")
    >>> check(Path("scratch"), want)  # doctest: +SKIP
    CheckerRun(returncode=0, stdout='PASSED: Model checker completed successfully', ...)
    """
    run = run_model(directory, expectation.model, *expectation.options)
    if run.returncode != 0:
        pytest.fail(
            f"{expectation.model} compiles: exit {run.returncode}\n{run.stdout}"
        )
    if expectation.verdict not in run.stdout:
        pytest.fail(f"{expectation.model}: {expectation.why}\n{run.stdout}")
    if (
        expectation.counts is not None
        and (explored := state_counts(run.stdout)) != expectation.counts
    ):
        pytest.fail(
            f"{expectation.model}: {expectation.why}; "
            f"expected {expectation.counts}, the run explored {explored}"
        )
    if expectation.trace and (names := link_names(error_graph(run))) != list(
        expectation.trace
    ):
        pytest.fail(
            f"{expectation.model}: {expectation.why}; "
            f"expected the trace {list(expectation.trace)}, the run showed {names}"
        )
    return run


def paired(
    directory: Path, *, should_pass: Expectation, should_fail: Expectation
) -> CheckerRun:
    """Run a pair of models whose verdicts differ, and return the failure.

    Fairness questions, and several others, are answered by a pair: one model
    shows the property holds and the near-identical model shows it does not.
    Running both in one call states the pair as a unit. The two runs go in
    their own subdirectories so neither sees the other's ``out/`` tree.

    Parameters
    ----------
    directory : Path
        A directory owned by this test.
    should_pass : Expectation
        The model that must pass, and why.
    should_fail : Expectation
        The model that must fail, and why.

    Returns
    -------
    CheckerRun
        The failing run, whose counterexample the caller usually inspects.
    """
    check(directory / "passing", should_pass)
    return check(directory / "failing", should_fail)
