"""The judgement truth table, over every expectation and every result.

``judge`` is the one place a checker run and a configuration meet, and a
mistake there is silent: a wrong verdict makes a suite pass while checking
nothing. The table is therefore exhaustive rather than illustrative, and it is
generated: the row labels are the domain's own unions, every generated row has
a classified verdict, and every verdict the domain declares is reached by a
named test here. A new result variant, expectation type or verdict therefore
cannot be added without classifying it.

Every test here was written before its module was implemented (EP-M2), so it
pins a contract the stub could not satisfy.
"""

import typing as typ

import pytest
from modelcheck_cases import (
    PROBE,
    PROPERTY,
    configuration,
    expectation_of,
    result_of,
    spec,
    trace,
)

if typ.TYPE_CHECKING:
    from modelcheck.domain import CheckResult, Expectation, Judgement

EXPECTATION_KINDS = ("holds", "fails_as", "reachable")
"""The three expectation types, named as :func:`expectation_of` builds them."""

RESULT_VARIANTS = ("passed", "counterexample", "stopped", "errored")
"""The four outcomes a run can have."""

PROBE_STATES = (True, False)
"""Whether a pass reports the probe as satisfied."""

# The verdict each row must produce. Writing every row out is the point: the
# table is the specification, and a reader can audit it against T2 without
# running anything. A pass appears twice for each kind because only a
# ``Reachable`` expectation reads the probe tuple; for the other two kinds the
# two rows must agree, and writing both is what shows it.
EXPECTED_VERDICTS: dict[tuple[str, str, bool], str] = {
    ("holds", "passed", True): "holds",
    ("holds", "passed", False): "holds",
    ("holds", "counterexample", True): "unexpected counterexample",
    ("holds", "stopped", True): "stopped",
    ("holds", "errored", True): "error",
    ("fails_as", "passed", True): "expected counterexample missing",
    ("fails_as", "passed", False): "expected counterexample missing",
    ("fails_as", "counterexample", True): "counterexample as expected",
    ("fails_as", "stopped", True): "stopped",
    ("fails_as", "errored", True): "error",
    ("reachable", "passed", True): "reached",
    ("reachable", "passed", False): "unreachable",
    ("reachable", "counterexample", True): "unexpected counterexample",
    ("reachable", "stopped", True): "stopped",
    ("reachable", "errored", True): "error",
}
"""The verdict every row of the truth table must produce."""

MET = frozenset({"holds", "counterexample as expected", "reached"})
"""The verdict values that satisfy their expectation."""

EXTRA_VERDICTS = frozenset({"state space below floor", "wrong counterexample kind"})
"""Verdicts the table cannot produce, and the tests below that reach them.

The table fixes the floor at one state and always fails on the property and
kind the expectation names, so these two need a configuration and a result
that deliberately depart from it.
"""


def _judge_row(
    kind: str, variant: str, probe: bool
) -> tuple[Expectation, CheckResult, Judgement]:
    """Build and judge one row of the truth table.

    Both the parametrized rows and the exhaustive sweep judge the same table,
    and the table only means anything if the two read a row the same way.
    Judging through one helper is what keeps them from disagreeing about what a
    row even is.

    The inputs come back with the judgement because the row test asserts that
    the judgement carries exactly the objects it was built from.

    Parameters
    ----------
    kind : str
        The expectation kind to build.
    variant : str
        The result variant to build.
    probe : bool
        Whether a pass reports the probe as satisfied.

    Returns
    -------
    tuple[Expectation, CheckResult, Judgement]
        The expectation, the result, and the judgement of one against the
        other.
    """
    from modelcheck import expectations

    expectation = expectation_of(kind)
    result = result_of(variant, probe_satisfied=probe)
    judgement = expectations.judge(
        configuration("head", expectations=(expectation,)),
        expectation,
        spec(),
        result,
    )
    return expectation, result, judgement


def _rows() -> list[tuple[str, str, bool]]:
    """Build the exhaustive row set for the truth table.

    The probe axis only changes a pass. Every other row pairs one result with
    one expectation kind, where the tuple cannot affect the verdict, so the
    axis is fixed to the satisfied state rather than multiplying rows that must
    agree.

    Returns
    -------
    list[tuple[str, str, bool]]
        One row per expectation kind, result variant and probe state.
    """
    return [
        (kind, variant, probe)
        for kind in EXPECTATION_KINDS
        for variant in RESULT_VARIANTS
        for probe in (PROBE_STATES if variant == "passed" else PROBE_STATES[:1])
    ]


@pytest.mark.parametrize(("kind", "variant", "probe"), _rows())
def test_judge_returns_the_classified_verdict(
    kind: str, variant: str, probe: bool
) -> None:
    """Every expectation kind against every result yields its classified verdict.

    Asserting the verdict rather than a boolean is what makes the table
    readable, and what distinguishes "the property was violated" from "the run
    never got far enough to say". ``is_met`` is checked against the same row so
    the two can never disagree.
    """
    expectation, result, judgement = _judge_row(kind, variant, probe)

    assert str(judgement.verdict) == EXPECTED_VERDICTS[kind, variant, probe], (
        "the row is classified as the table says"
    )
    assert judgement.configuration == "head", "the configuration is named"
    assert judgement.expectation is expectation, "the expectation is carried"
    assert judgement.result is result, "and so is the result"
    assert judgement.fairness_in_force == ("F1", "F2"), "as is its fairness"
    assert judgement.is_met is (str(judgement.verdict) in MET), (
        "and is_met agrees with the verdict"
    )


def test_the_truth_table_classifies_every_row() -> None:
    """Every generated row is judged, classified, and every verdict is reached.

    This is T2's non-vacuity clause. The row labels are compared with the
    members of the domain's unions, so a new result variant or expectation
    type appears in a failing assertion here rather than being silently
    untested; and the verdicts ``judge`` actually returns are compared with the
    verdicts the domain declares, so a new verdict cannot be added without a
    row that reaches it.
    """
    from modelcheck import domain

    assert set(EXPECTATION_KINDS) == {
        _snake(name) for name in _union_members(domain.Expectation)
    }, "every expectation type has a row label"
    assert set(RESULT_VARIANTS) == {
        _snake(name) for name in _union_members(domain.CheckResult)
    }, "every result variant has a row label"

    produced = {row: str(_judge_row(*row)[2].verdict) for row in _rows()}

    assert produced == EXPECTED_VERDICTS, (
        "every generated row is classified as the table says"
    )
    assert set(produced.values()) | EXTRA_VERDICTS == {
        str(verdict) for verdict in domain.Verdict
    }, "every declared verdict is reached by this module"


def _judge_counterexample(failed: str, kind: str) -> Judgement:
    """Judge a ``FailsAs`` expectation against a counterexample that misses it.

    Parameters
    ----------
    failed : str
        The property the run's counterexample names.
    kind : str
        The counterexample kind the run reports.

    Returns
    -------
    domain.Judgement
        The judgement, which every caller here expects to be unmet.
    """
    from modelcheck import domain, expectations

    expectation = domain.FailsAs(PROPERTY, domain.CounterexampleKind.LASSO)
    result = domain.Counterexample(
        failed, domain.CounterexampleKind(kind), trace(), 9, 0.2
    )
    return expectations.judge(
        configuration("head", expectations=(expectation,)), expectation, spec(), result
    )


def test_a_counterexample_to_another_property_does_not_meet_fails_as() -> None:
    """The named property must be the one that failed.

    A configuration whose expected failure is ``L3`` and whose run fails on
    ``L1`` has not found the defect it was built to find. Nothing here is the
    expected failure, so the verdict is ``unexpected counterexample``.
    """
    from modelcheck import domain

    judgement = _judge_counterexample("L1", "lasso")

    assert judgement.verdict is domain.Verdict.UNEXPECTED, "another property failed"
    assert not judgement.is_met, "so the expectation is unmet"


def test_a_counterexample_of_another_kind_does_not_meet_fails_as() -> None:
    """The right property with the wrong kind of path is its own verdict.

    A stutter where the incident produced a lasso means the defect now
    manifests differently, which is worth reporting as such rather than as a
    missing counterexample.
    """
    from modelcheck import domain

    judgement = _judge_counterexample(PROPERTY, "stutter")

    assert judgement.verdict is domain.Verdict.WRONG_KIND, "the kind differs"
    assert not judgement.is_met, "so the expectation is unmet"


def test_a_pass_below_the_floor_proves_nothing() -> None:
    """A pass below the configuration's floor is unmet; a violation is not.

    Exploration that stopped short of the claimed space cannot show a property
    holds, so the floor turns such a pass into ``state space below floor``. A
    counterexample found below the floor is still a counterexample — the
    forbidden state was reached — so the floor does not suppress one.
    """
    from modelcheck import domain, expectations

    expectation = domain.Holds("S1")
    bounded = configuration("head", min_states=1000, expectations=(expectation,))

    small_pass = expectations.judge(
        bounded, expectation, spec(), domain.Passed(300, 0.4, ())
    )
    assert small_pass.verdict is domain.Verdict.BELOW_FLOOR, "too small to prove"
    assert not small_pass.is_met, "so the pass proves nothing"

    big_pass = expectations.judge(
        bounded, expectation, spec(), domain.Passed(1400, 2.0, ())
    )
    assert big_pass.verdict is domain.Verdict.HOLDS, "above the floor it holds"

    small_failure = expectations.judge(
        bounded,
        expectation,
        spec(),
        domain.Counterexample(
            "S1", domain.CounterexampleKind.FINITE, trace(), 300, 0.4
        ),
    )
    assert small_failure.verdict is domain.Verdict.UNEXPECTED, (
        "a violation below the floor is still a violation"
    )


def test_a_reachable_expectation_reads_only_its_own_probe() -> None:
    """FizzBee reports every unsatisfied probe, so membership is what counts.

    A run can report one probe satisfied and another not; reading the whole
    tuple rather than membership would let an unrelated probe decide this
    expectation's verdict.
    """
    from modelcheck import domain, expectations

    expectation = domain.Reachable(PROBE)
    bounded = configuration("head", expectations=(expectation,))

    reached = expectations.judge(
        bounded,
        expectation,
        spec(),
        domain.Passed(40, 0.5, ("AnotherProbe",)),
    )
    assert reached.verdict is domain.Verdict.REACHED, "another probe does not count"

    unreachable = expectations.judge(
        bounded,
        expectation,
        spec(),
        domain.Passed(40, 0.5, (PROBE, "AnotherProbe")),
    )
    assert unreachable.verdict is domain.Verdict.UNREACHABLE, "its own probe does"


def test_judging_against_another_configurations_spec_is_a_defect() -> None:
    """A judgement never quotes fairness a different run carried.

    The materialized specification is checked against the configuration rather
    than trusted, because the fairness a report names is the one thing a
    reader uses to judge how much the result is worth.
    """
    from modelcheck import domain, expectations

    expectation = domain.Holds(PROPERTY)
    wrong = domain.MaterializedSpec("incident-3", "", ("F1",))

    with pytest.raises(ValueError, match="incident-3"):
        expectations.judge(
            configuration("head", expectations=(expectation,)),
            expectation,
            wrong,
            domain.Passed(9, 0.1, ()),
        )


def _snake(name: str) -> str:
    """Return a class name in the lower-case form the row labels use.

    Parameters
    ----------
    name : str
        A class name such as ``FailsAs``.

    Returns
    -------
    str
        The label such as ``fails_as``.
    """
    characters: list[str] = []
    for character in name:
        if character.isupper() and characters:
            characters.append("_")
        characters.append(character.lower())
    return "".join(characters)


def _union_members(alias: typ.TypeAliasType) -> tuple[str, ...]:
    """Return the class names of a PEP 695 union alias's members.

    Parameters
    ----------
    alias : typing.TypeAliasType
        A ``type`` statement's alias, such as ``CheckResult``.

    Returns
    -------
    tuple[str, ...]
        The member class names, in declaration order.
    """
    return tuple(member.__name__ for member in typ.get_args(alias.__value__))
