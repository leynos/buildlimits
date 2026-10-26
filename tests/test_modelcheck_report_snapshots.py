"""The rendered reports are pinned as whole documents, one per verdict.

``test_modelcheck_report`` asserts each column against its field, which names
what moved when one does. That is the better failure, but it cannot see a
column that vanished: a report that dropped the fairness column entirely would
still satisfy every assertion about the columns that remained.

These snapshots are the opposite trade. They pin the whole rendering — every
line, every column, every word of the summary — so a change to any of it has
to be looked at, and a reader comparing the snapshot with the code can see the
document a maintainer sees. Each snapshot is paired with assertions that say
what the document *means*: the verdict each judgement earned and the exit
status the matrix would have produced. A snapshot alone would pin the wording
without pinning the judgement, and the wording is the cheaper of the two to be
wrong about.

Nothing here needs normalizing. The renderers read no clock, no directory and
no environment: a pass contributes its state count, a counterexample its kind
and step count, and drift its dotted production paths. The one number that
does vary between runs, ``elapsed_seconds``, is carried on the result and never
rendered — a duration in a report is noise a reviewer learns to skip, and it
would make every snapshot churn.

Every ``Verdict`` member is covered. A verdict the table forgot fails the
completeness assertion rather than quietly going unrendered.
"""

import dataclasses as dc
import typing as typ

import pytest
from modelcheck_cases import configuration, expectation_of, render, result_of, spec

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from syrupy.assertion import SnapshotAssertion

    from modelcheck.domain import Judgement


_CONFIGURATION = "incident-1-cap2-strong-nobackoff"
"""The configuration name the snapshots render.

It is the plan's sample configuration name, and it is exactly as wide as the
report's configuration column — the boundary the padding is written for. A
name one character longer moves the rest of its line right; the verdict column
does just that in the snapshot, where ``expected counterexample missing`` is
wider than its own column, so the snapshot pins the overflow behaviour as well
as the alignment.
"""

_FAIRNESS = ("F1", "F2", "F5", "F6")
"""The fairness tags the snapshots' runs carry, in the order they are listed."""


def _case(  # pylint: disable=too-many-arguments  # one keyword per judgement field.
    expectation_kind: str,
    variant: str,
    *,
    min_states: int = 1,
    probe_satisfied: bool = True,
    counterexample_kind: object = None,
) -> Judgement:
    """Judge one expectation kind against one result variant.

    Parameters
    ----------
    expectation_kind : str
        The expectation kind: ``holds``, ``fails_as`` or ``reachable``.
    variant : str
        The result variant: ``passed``, ``counterexample``, ``stopped`` or
        ``errored``.
    min_states : int
        The configuration's exploration floor.
    probe_satisfied : bool
        Whether a pass reports the probe as satisfied.
    counterexample_kind : CounterexampleKind | None
        The kind to give a counterexample before it is judged, which is how
        the ``wrong_kind`` case is built. ``None`` keeps the recorded lasso.

    Returns
    -------
    Judgement
        The judged pair, carrying :data:`_FAIRNESS`.
    """
    from modelcheck import expectations

    expectation = expectation_of(expectation_kind)
    result = result_of(variant, probe_satisfied=probe_satisfied)
    if counterexample_kind is not None:
        result = dc.replace(result, kind=counterexample_kind)
    return expectations.judge(
        configuration(
            _CONFIGURATION, min_states=min_states, expectations=(expectation,)
        ),
        expectation,
        spec(_CONFIGURATION, fairness=_FAIRNESS),
        result,
    )


@pytest.fixture
def judgements() -> list[Judgement]:
    """Return one judgement per verdict, in the report's own order.

    A fixture rather than a plain helper because three tests read the same
    table: one checks it covers the enum, one renders it whole, and one counts
    what it summarises. Handing the table in says that each of them is a
    statement about the same ten cases, where three calls to a builder would
    read as three tables that happened to be built alike.

    Returns
    -------
    list[Judgement]
        Every verdict the domain declares, with a judgement that earns it. The
        order is the table's, and the completeness assertion compares the
        judgements' own verdict names against the enum, so a verdict added to
        the domain without a case here fails rather than going unrendered.
    """
    from modelcheck.domain import CounterexampleKind

    return [
        _case("holds", "passed"),
        _case("fails_as", "counterexample"),
        _case("reachable", "passed"),
        _case("fails_as", "passed"),
        _case(
            "fails_as", "counterexample", counterexample_kind=CounterexampleKind.FINITE
        ),
        _case("holds", "counterexample"),
        _case("reachable", "passed", probe_satisfied=False),
        _case("holds", "passed", min_states=800),
        _case("holds", "stopped"),
        _case("holds", "errored"),
    ]


def _named(cases: cabc.Sequence[Judgement]) -> dict[str, Judgement]:
    """Key each case by the verdict it earned.

    Parameters
    ----------
    cases : collections.abc.Sequence[Judgement]
        The cases to index.

    Returns
    -------
    dict[str, Judgement]
        One entry per verdict name, which is the shape the completeness
        assertion compares against the enum. A duplicate verdict would collapse
        two cases into one, and the count assertion below notices that.
    """
    return {case.verdict.name.lower(): case for case in cases}


def test_the_case_table_covers_every_verdict(judgements: list[Judgement]) -> None:
    """Every ``Verdict`` member is exercised, and each case earns its own name.

    A verdict added to the domain without a case here would render untested and
    the snapshot below would silently keep describing the ten it knew. The
    index is keyed by each case's own verdict, so a case whose name and verdict
    disagreed would appear under the name it earned — and the comparison is a
    whole mapping, so a failure names the verdict that is wrong.
    """
    from modelcheck.domain import Verdict

    assert len(_named(judgements)) == len(judgements), "no two cases earn one verdict"
    assert set(_named(judgements)) == {verdict.name.lower() for verdict in Verdict}, (
        "one case per verdict"
    )


def test_every_verdict_renders_a_line(
    snapshot: SnapshotAssertion, judgements: list[Judgement]
) -> None:
    """The rendered matrix is pinned whole, one line per verdict.

    The lines are in the table's order, so the snapshot reads as a matrix
    rather than as an unordered set. Each line also begins with its own record
    and ends with its own fairness, which is what makes a snapshot regenerated
    after a column change still say which column is which.
    """
    rendered = render(judgements)
    lines = rendered.splitlines()

    assert [
        (line.startswith(judgement.configuration), line.endswith(_fairness_line()))
        for judgement, line in zip(judgements, lines, strict=False)
    ] == [(True, True)] * len(judgements), "every line carries its own columns"
    assert rendered == snapshot, "the matrix is pinned whole"


def _fairness_line() -> str:
    """Return the fairness column every snapshot line ends with.

    Returns
    -------
    str
        The column as :func:`modelcheck.report.render_report` spells it for
        :data:`_FAIRNESS`.
    """
    return f"fairness: {', '.join(_FAIRNESS)}"


def test_the_summary_counts_unmet_verdicts(
    snapshot: SnapshotAssertion, judgements: list[Judgement]
) -> None:
    """The summary's two counts are pinned, and agree with the verdicts.

    The counts are the line a reviewer reads first and the only place the
    report states a total. Pinning the line catches a change to its wording;
    asserting the numbers catches a change to what is counted.
    """
    from modelcheck import cli

    summary = render(judgements).splitlines()[-1]
    met = sum(1 for judgement in judgements if judgement.is_met)

    assert (met, len(judgements) - met) == (3, 7), "three met, seven unmet"
    assert cli.exit_status(judgements) == cli.EXIT_UNMET, "an unmet matrix exits 1"
    assert summary == snapshot, "and the summary line is pinned"


def test_a_met_matrix_exits_zero(snapshot: SnapshotAssertion) -> None:
    """A matrix of met expectations reports every one met and exits ``0``.

    The zero case is pinned separately because the large matrix above can only
    show the failing status: a run whose summary said ``0 unmet`` and whose
    status was ``1`` would satisfy every assertion about the counts.
    """
    from modelcheck import cli

    judgements = [_case("holds", "passed"), _case("reachable", "passed")]

    rendered = render(judgements)

    assert cli.exit_status(judgements) == cli.EXIT_MET, "a met matrix exits 0"
    assert rendered == snapshot, "and the zero case's report is pinned separately"


@pytest.mark.parametrize(
    ("unmapped", "stale", "excluded"),
    [
        pytest.param((), (), (), id="clean"),
        pytest.param(("buildlimits.effects.Pause",), (), (), id="unmapped"),
        pytest.param((), ("buildlimits.recovery_stop.request",), (), id="stale"),
        pytest.param((), (), ("buildlimits.effects.Exit",), id="excluded"),
    ],
)
def test_the_drift_report_is_pinned(
    snapshot: SnapshotAssertion,
    unmapped: tuple[str, ...],
    stale: tuple[str, ...],
    excluded: tuple[str, ...],
) -> None:
    """Each account a drift report can carry is pinned with its conclusion.

    The conclusion is asserted beside the snapshot because it is what a reader
    acts on: an exclusion-only report must still read as a clean comparison,
    and a report with a finding must count it. A snapshot regenerated in a
    moment of inattention would pin the wrong conclusion just as happily.
    """
    from modelcheck import report
    from modelcheck.drift import DriftReport

    found = DriftReport(unmapped, stale, excluded)

    rendered = report.render_drift(found)

    assert found.is_clean is (not unmapped and not stale), "only findings dirty it"
    assert rendered == snapshot, "and the account is pinned with it"
