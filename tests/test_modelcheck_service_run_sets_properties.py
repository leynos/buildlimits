"""A matrix's run set is judged by the same count rules whatever its size.

:mod:`tests.test_modelcheck_service_properties` states each rule of the
state-count comparison for a pair of runs, because a pair is the smallest set
that can disagree. A matrix builds one run per expected failure, so a
configuration may declare three or four defects, and the rules have to hold
there too: a silent run leaves the configuration uncompared, disagreeing
completed counts refuse every row with one detail naming each count, and an
agreeing set is judged row by row.

Drawing the whole set is what says the comparison is about the runs rather than
about the sizes somebody wrote down. One property draws three or four names,
with a run of each kind and, in turn, no run that reports nothing and one that
does, so the sets a matrix builds are exercised rather than only the smallest
one.

Two of the drawn parameters steer towards the branches that random values would
otherwise rarely reach. ``silence`` makes exactly one run report nothing, and
``None`` makes every run report; when every run reports, the first two are
completed runs, so a disagreement is the ordinary case. ``agree`` pins those
completed counts to one value, which is what reaches the agreeing branch;
drawn independently, two counts almost never meet.
"""

import collections.abc as cabc

from hypothesis import given
from hypothesis import strategies as st
from modelcheck_service_cases import (
    Answers,
    bounded_configuration,
    counterexample,
    mismatch,
)
from modelcheck_source_doubles import FixedSource

from modelcheck import domain, service
from modelcheck.expectations import subject

type _Plan = tuple[domain.CounterexampleKind, int, str | None]
"""One run's kind, the count it reports, and how it fails to report one."""

_NAMES = st.lists(
    st.sampled_from(("S1", "S2", "S3", "S4")), min_size=3, max_size=4, unique=True
)
"""Three or four distinct expected failures, in declaration order."""

_RUN_PLAN = st.tuples(
    st.sampled_from([
        domain.CounterexampleKind.LASSO,
        domain.CounterexampleKind.FINITE,
        domain.CounterexampleKind.DEADLOCK,
    ]),
    st.integers(min_value=0, max_value=50),
    st.sampled_from([None, "errored", "stopped"]),
)
"""One run's kind, the count it reports, and how it fails to report one."""


def _model(names: cabc.Sequence[str]) -> str:
    """Return a model declaring one assertion for each expected failure.

    Parameters
    ----------
    names : collections.abc.Sequence[str]
        The expected failures the configuration declares.

    Returns
    -------
    str
        The model text, with one always assertion per name. The bodies match,
        which is a shape a matrix meets in practice; the run keeps only the
        assertion it is judged by, so the names identify the runs.
    """
    assertions = "".join(
        f"always assertion {name}:\n  return n < 10\n\n" for name in names
    )
    return f"action Init:\n  n = 0\n\n{assertions}"


def _silent(absence: str | None) -> domain.CheckResult | None:
    """Return the result a run reports instead of a count, if it reports none.

    Parameters
    ----------
    absence : str | None
        ``"errored"`` or ``"stopped"`` for a run that reports nothing, and
        ``None`` for a run whose count is read.

    Returns
    -------
    domain.CheckResult | None
        The error or stop, or ``None`` when the run reports a count.
    """
    match absence:
        case "errored":
            return domain.Errored("no verdict line")
        case "stopped":
            return domain.Stopped("interrupted")
        case _:
            return None


def _expected_verdict(absence: str | None) -> domain.Verdict:
    """Return the verdict a run with the given absence reports.

    Parameters
    ----------
    absence : str | None
        ``"errored"`` or ``"stopped"`` for a run that reports nothing, and
        ``None`` for a run whose count is read.

    Returns
    -------
    domain.Verdict
        The verdict that run's own outcome earns, whichever the counts say.
    """
    match absence:
        case "errored":
            return domain.Verdict.ERROR
        case "stopped":
            return domain.Verdict.STOPPED
        case _:
            return domain.Verdict.AS_EXPECTED


def _completed(plan: _Plan) -> bool:
    """Return whether a planned run completes its exploration and reports.

    Parameters
    ----------
    plan : _Plan
        One run's kind, count and absence.

    Returns
    -------
    bool
        Whether that run's count can be compared with another's. A finite or
        deadlock counterexample stops at the violation, and a run that reports
        nothing has no count at all.
    """
    kind, _, absence = plan
    return kind is domain.CounterexampleKind.LASSO and absence is None


def _steered(
    names: cabc.Sequence[str],
    plans: cabc.Sequence[_Plan],
    *,
    agree: bool,
    silence: str | None,
) -> tuple[dict[str, _Plan], dict[str, int]]:
    """Return the planned runs and their counts after steering the draw.

    Parameters
    ----------
    names : collections.abc.Sequence[str]
        The expected failures the configuration declares.
    plans : collections.abc.Sequence[_Plan]
        One drawn plan per name, in declaration order.
    agree : bool
        Whether the completed runs are given one shared count, which is what
        reaches the agreeing branch.
    silence : str | None
        ``"errored"`` or ``"stopped"`` to make exactly one run report nothing
        instead of a count, and ``None`` to leave every drawn plan as it was.

    Returns
    -------
    tuple[dict[str, _Plan], dict[str, int]]
        The plan for each name, and the count each run reports. When no run is
        silent the first two are made completed runs, so their counts are
        always compared.
    """
    plan = dict(zip(names, plans, strict=False))
    if silence is not None:
        last = names[-1]
        kind, count, _ = plan[last]
        plan[last] = (kind, count, silence)
    else:
        for name in names[:2]:
            _, count, absence = plan[name]
            plan[name] = (domain.CounterexampleKind.LASSO, count, absence)
    completed = [name for name in names if _completed(plan[name])]
    shared = plan[completed[0]][1] if agree and completed else None
    counts = {
        name: shared if shared is not None and _completed(plan[name]) else plan[name][1]
        for name in names
    }
    return plan, counts


def _results(
    names: cabc.Sequence[str], plan: dict[str, _Plan], counts: dict[str, int]
) -> dict[str, domain.CheckResult]:
    """Return the result each planned run reports.

    Parameters
    ----------
    names : collections.abc.Sequence[str]
        The expected failures the configuration declares.
    plan : dict[str, _Plan]
        The plan for each name.
    counts : dict[str, int]
        The count each run reports.

    Returns
    -------
    dict[str, domain.CheckResult]
        The error or stop a silent run reports, and a counterexample carrying
        its count for every other run.
    """
    results: dict[str, domain.CheckResult] = {}
    for name in names:
        kind, _, absence = plan[name]
        silent = _silent(absence)
        results[name] = (
            silent if silent is not None else counterexample(name, kind, counts[name])
        )
    return results


@given(
    names=_NAMES,
    plans=st.lists(_RUN_PLAN, min_size=4, max_size=4),
    agree=st.booleans(),
    silence=st.sampled_from([None, "errored", "stopped"]),
)
def test_a_variable_run_set_is_judged_by_its_completed_counts(
    names: list[str],
    plans: list[_Plan],
    *,
    agree: bool,
    silence: str | None,
) -> None:
    """A set of three or four runs obeys the same count rules as a pair.

    The examples state the comparison for two runs, and a matrix may build
    more: one run per expected failure. Whatever the set, a silent run leaves
    the configuration uncompared, disagreeing completed counts refuse every
    row with one detail naming each count, and an agreeing set is judged row
    by row.
    """
    plan, counts = _steered(names, plans, agree=agree, silence=silence)
    configuration = bounded_configuration(
        expectations=tuple(domain.FailsAs(name, plan[name][0]) for name in names)
    )
    judgements = service.run_matrix(
        FixedSource((configuration,), source=_model(names)),
        Answers(_results(names, plan, counts)),
    )
    verdicts = {
        subject(judgement.expectation): judgement.verdict for judgement in judgements
    }
    assert set(verdicts) == set(names), "every run has one row"

    detail = mismatch(judgements)
    completed = [counts[name] for name in names if _completed(plan[name])]
    if any(plan[name][2] is not None for name in names) or len(set(completed)) <= 1:
        assert detail is None, "a silent or agreeing set is not a disagreement"
        for name in names:
            assert verdicts[name] is _expected_verdict(plan[name][2]), (
                f"{name} keeps its own verdict"
            )
        return

    assert set(verdicts.values()) == {domain.Verdict.ERROR}, "no run can be judged"
    assert detail is not None, "the rows name the disagreement"
    for name in names:
        assert f"expecting {name}: {counts[name]} states" in detail, (
            f"{name}'s count is named"
        )
