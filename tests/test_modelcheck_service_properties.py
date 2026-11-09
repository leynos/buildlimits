"""Every generated run set is judged by the same count-comparison rules.

:mod:`tests.test_modelcheck_service` states each rule of the state-count
comparison once: two completed runs that disagree make every row an error, a
finite or deadlock counterexample is left out of the comparison, and a run that
reported no count leaves the whole configuration uncompared. Examples can only
say that those rules hold for the combinations somebody wrote down. What the
comparison promises is stronger — it holds whatever the configuration's
expectations are, in whatever order the file declares them — and that is a
claim about every run set, so it is drawn from strategies here.

The checker double is keyed by assertion rather than by call order, because the
service builds its runs in the order the configuration declares them. A run is
answered by the assertion its materialized specification still carries, so the
same double answers a reversed configuration with the same results and the
property can compare the two outcomes.

Only completed kinds are compared. A lasso completes its exploration, while a
finite or deadlock counterexample stops at the violation and prints how far the
search had got, so its count says nothing about the size of the graph; the
properties draw both kinds to keep that distinction under test rather than
assuming it.
"""

import collections.abc as cabc

from hypothesis import given
from hypothesis import strategies as st
from modelcheck_cases import trace
from modelcheck_service_cases import bounded_configuration, source

from modelcheck import domain, service
from modelcheck.expectations import subject

_PARTIAL = st.sampled_from([
    domain.CounterexampleKind.FINITE,
    domain.CounterexampleKind.DEADLOCK,
])
"""Kinds whose count is the search's progress rather than the graph's size."""

_FAILURES = st.sampled_from([("S1", "S2"), ("S2", "S1")])
"""Two distinct expected failures, in either declaration order."""

_COUNTS = st.tuples(st.integers(min_value=0, max_value=50), st.integers(1, 50))
"""A count and a positive offset, so the two counts are always distinct."""


class _Answers:
    """A checker answering each targeted run from a result held for it.

    Parameters
    ----------
    targeted : dict[str, domain.CheckResult]
        The result the run for each expected failure reports, keyed by the
        assertion that run keeps.
    positive : domain.CheckResult | None
        The result the run holding the assertions expected to hold reports, or
        ``None`` when the configuration expects only defects and so has no
        positive run.
    """

    def __init__(
        self,
        targeted: dict[str, domain.CheckResult],
        *,
        positive: domain.CheckResult | None = None,
    ) -> None:
        """Hold the results and the assertion each is keyed by."""
        self._targeted = targeted
        self._positive = positive

    def check(self, spec: domain.MaterializedSpec) -> domain.CheckResult:
        """Answer the run whose specification still carries an assertion.

        Parameters
        ----------
        spec : domain.MaterializedSpec
            The specification to answer. A targeted run retains only the
            assertions it is judged by, so the assertion present identifies
            which expected failure this run was built for.

        Returns
        -------
        domain.CheckResult
            The result held for that run.

        Raises
        ------
        AssertionError
            If the specification carries no expected failure and the
            configuration has no positive run, which the service never asks.
        """
        for name, result in self._targeted.items():
            if f"assertion {name}:" in spec.text:
                return result
        if self._positive is None:
            raise AssertionError("a positive run this configuration does not have")
        return self._positive


def _counterexample(
    name: str, kind: domain.CounterexampleKind, count: int
) -> domain.CheckResult:
    """Return a counterexample of ``kind`` that reported ``count`` states.

    Parameters
    ----------
    name : str
        The assertion the counterexample violates.
    kind : domain.CounterexampleKind
        The shape of the path the counterexample reports.
    count : int
        The number of states the run reported.

    Returns
    -------
    domain.CheckResult
        The counterexample, with the graph's trace left empty because these
        tests are about the counts rather than the path.
    """
    return domain.Counterexample(name, kind, trace(), count, 0.1)


def _mismatch(judgements: cabc.Iterable[domain.Judgement]) -> str | None:
    """Return the count-mismatch detail a judgement set carries, if any.

    Parameters
    ----------
    judgements : collections.abc.Iterable[domain.Judgement]
        The judgements one matrix produced.

    Returns
    -------
    str | None
        The one mismatch detail, or ``None`` when the configuration was not
        refused for disagreeing counts. More than one detail would mean the
        rows disagree about why they are errors, which is itself a defect.
    """
    details = {
        judgement.result.detail
        for judgement in judgements
        if isinstance(judgement.result, domain.Errored)
        and "explored different state counts" in judgement.result.detail
    }
    assert len(details) <= 1, "every row names the same mismatch"
    return details.pop() if details else None


def _judged_as_lassos(
    failures: tuple[str, str], results: dict[str, domain.CheckResult]
) -> tuple[domain.Judgement, ...]:
    """Return the matrix's judgements for two failures expected as lassos.

    Parameters
    ----------
    failures : tuple[str, str]
        The two expected failures, in the order the configuration declares
        them.
    results : dict[str, domain.CheckResult]
        The result the run for each expected failure reports, keyed by that
        failure.

    Returns
    -------
    tuple[domain.Judgement, ...]
        One judgement per expected failure, in declaration order.
    """
    configuration = bounded_configuration(
        expectations=tuple(
            domain.FailsAs(name, domain.CounterexampleKind.LASSO) for name in failures
        )
    )
    return service.run_matrix(source(configuration), _Answers(results))


@given(failures=_FAILURES, counts=_COUNTS)
def test_completed_runs_that_disagree_make_every_row_an_error(
    failures: tuple[str, str], counts: tuple[int, int]
) -> None:
    """Completed runs with different counts cannot judge one expectation.

    Stripping assertions must not change the state graph, so a disagreement
    means the runs explored different models. Both counts are named in one
    detail that every row shares, whatever the order the file declared its
    expectations in.
    """
    first, second = failures
    earlier, later = counts[0], counts[0] + counts[1]
    judgements = _judged_as_lassos(
        failures,
        {
            first: _counterexample(first, domain.CounterexampleKind.LASSO, earlier),
            second: _counterexample(second, domain.CounterexampleKind.LASSO, later),
        },
    )

    assert [judgement.verdict for judgement in judgements] == [
        domain.Verdict.ERROR,
        domain.Verdict.ERROR,
    ], "neither run can be judged"
    detail = _mismatch(judgements)
    assert detail is not None, "the rows name the disagreement"
    assert f"expecting {first}: {earlier} states" in detail, "the first count is named"
    assert f"expecting {second}: {later} states" in detail, "and so is the second"


@given(
    failures=_FAILURES,
    combination=_COUNTS,
    kind=_PARTIAL,
)
def test_partial_counts_are_left_out_of_the_comparison(
    failures: tuple[str, str],
    combination: tuple[int, int],
    kind: domain.CounterexampleKind,
) -> None:
    """A run that stopped at a violation explored no fixed number of states.

    The count a finite or deadlock counterexample prints is how far the search
    had got, so comparing it with a completed run's would refuse a
    configuration for finding exactly the defect it declared it expected.
    """
    first, second = failures
    completed, partial = combination
    configuration = bounded_configuration(
        expectations=(
            domain.FailsAs(first, domain.CounterexampleKind.LASSO),
            domain.FailsAs(second, kind),
        )
    )

    judgements = service.run_matrix(
        source(configuration),
        _Answers({
            first: _counterexample(first, domain.CounterexampleKind.LASSO, completed),
            second: _counterexample(second, kind, completed + partial),
        }),
    )

    assert _mismatch(judgements) is None, "a partial count is not evidence"
    assert [judgement.verdict for judgement in judgements] == [
        domain.Verdict.AS_EXPECTED,
        domain.Verdict.AS_EXPECTED,
    ], "each run answers its own expectation"


@given(
    failures=_FAILURES,
    counts=_COUNTS,
    missing=st.sampled_from(["errored", "stopped"]),
    is_first=st.booleans(),
)
def test_a_run_without_a_count_leaves_the_configuration_uncompared(
    failures: tuple[str, str],
    counts: tuple[int, int],
    missing: str,
    *,
    is_first: bool,
) -> None:
    """A missing count is its own error, not evidence the graph changed.

    A run that reported nothing is already an error or a stop whose detail
    says what went wrong. Comparing only the runs that did report would claim
    an agreement this matrix never established, and replacing the silent run
    with a count mismatch would hide the cause, so the whole configuration is
    left uncompared.
    """
    first, second = failures
    completed, offset = counts
    absent = first if is_first else second
    silent: domain.CheckResult = (
        domain.Errored("no verdict line")
        if missing == "errored"
        else domain.Stopped("interrupted")
    )
    judgements = _judged_as_lassos(
        failures,
        {
            name: (
                silent
                if name == absent
                else _counterexample(
                    name, domain.CounterexampleKind.LASSO, completed + offset
                )
            )
            for name in (first, second)
        },
    )

    assert _mismatch(judgements) is None, "the silent run is not a disagreement"
    verdicts = {
        subject(judgement.expectation): judgement.verdict for judgement in judgements
    }
    expected = domain.Verdict.ERROR if missing == "errored" else domain.Verdict.STOPPED
    assert verdicts[absent] is expected, "the silent run keeps its own verdict"
    other = second if is_first else first
    assert verdicts[other] is domain.Verdict.AS_EXPECTED, "the other run is judged"


@given(failures=_FAILURES, counts=_COUNTS, second_kind=_PARTIAL)
def test_declaration_order_does_not_change_the_outcome(
    failures: tuple[str, str],
    counts: tuple[int, int],
    second_kind: domain.CounterexampleKind,
) -> None:
    """Reversing the expectations changes the rows' order, not their meaning.

    The report reads in the order the file declared, so the rows permute with
    it. Whether the configuration is refused for disagreeing counts, and what
    each expectation is judged to be, must not depend on that order.
    """
    first, second = failures
    completed, offset = counts
    kinds = {first: domain.CounterexampleKind.LASSO, second: second_kind}
    targeted = {
        first: _counterexample(first, domain.CounterexampleKind.LASSO, completed),
        second: _counterexample(second, second_kind, completed + offset),
    }

    def judged(order: tuple[str, str]) -> dict[str, domain.Verdict]:
        """Return each expectation's verdict for one declared order."""
        configuration = bounded_configuration(
            expectations=tuple(domain.FailsAs(name, kinds[name]) for name in order)
        )
        judgements = service.run_matrix(source(configuration), _Answers(targeted))
        return {
            subject(judgement.expectation): judgement.verdict
            for judgement in judgements
        }

    forward = judged((first, second))
    backward = judged((second, first))

    assert forward == backward, "the same expectations are judged the same way"
    assert set(forward) == {first, second}, "every expectation has one row"
