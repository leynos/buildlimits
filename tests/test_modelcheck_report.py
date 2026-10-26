"""Both reports are rendered by value, and their wording is a contract.

A reader scans the check report to decide whether to believe a run and the
drift report to decide whether the model still describes the code. A rendering
defect that dropped the fairness column would read as "no assumption was
made", and one that dropped a finding would read as a clean comparison, so
neither can be left to a subprocess's stream. Both renderers are pure and are
tested here directly, which also names the judgement that was misrendered
rather than only the run that produced it.

Columns are asserted by slicing, not by whole-line equality. A whole line
couples every field to every column width, so widening one column would fail
every assertion at once and say nothing about what moved.
"""

import re
import typing as typ

from modelcheck_cases import (
    PROBE,
    PROPERTY,
    configuration,
    expectation_of,
    render,
    result_of,
    spec,
)

if typ.TYPE_CHECKING:
    from modelcheck.domain import CheckResult, Expectation, Judgement

CONFIGURATION_WIDTH = 32
"""The minimum width of the configuration column."""

PROPERTY_WIDTH = 3
"""The minimum width of the property column."""

VERDICT_WIDTH = 27
"""The minimum width of the verdict column."""

_COLUMN_GAP = "  "
"""The separator between two columns.

Each field is padded to its minimum width and then joined with exactly this
much space, so a field that overflows its minimum pushes the rest of the line
right instead of being truncated.

Splitting on a *run* of two or more spaces, rather than on the separator
literal, is what recovers the fields: a short field's padding and the gap that
follows it merge into one run, so the literal splits inside the padding. No
field contains a run of two spaces of its own, which is what makes the
recovery exact.
"""

_GAP = re.compile(r" {2,}")
"""A run of two or more spaces, which is always a column boundary."""


def _judge(
    expectation: Expectation, result: CheckResult, *, fairness: tuple[str, ...] = ()
) -> Judgement:
    """Judge one result against one ``head`` expectation.

    Parameters
    ----------
    expectation : Expectation
        What the configuration expected.
    result : CheckResult
        What its run produced.
    fairness : tuple[str, ...]
        The fairness tags the run carried.

    Returns
    -------
    Judgement
        The judgement the two produce.
    """
    from modelcheck import expectations

    return expectations.judge(
        configuration("head", expectations=(expectation,)),
        expectation,
        spec("head", fairness=fairness),
        result,
    )


def _columns(line: str) -> tuple[str, str, str, str]:
    """Split one report line into its leading fields.

    Parameters
    ----------
    line : str
        One rendered line, without its newline.

    Returns
    -------
    tuple[str, str, str, str]
        The configuration, the property, the verdict and everything after
        them, which is the evidence and fairness columns joined.
    """
    first, second, third, *rest = _GAP.split(line, maxsplit=3)
    return first, second, third, _COLUMN_GAP.join(rest)


def _line_of(kind: str, variant: str, *, fairness: tuple[str, ...] = ()) -> str:
    """Judge one expectation-variant pair and render its single line.

    Parameters
    ----------
    kind : str
        The expectation kind to judge.
    variant : str
        The result variant to judge it against.
    fairness : tuple[str, ...]
        The fairness tags the run carried.

    Returns
    -------
    str
        The rendered report line, without its newline.
    """
    judgement = _judge(expectation_of(kind), result_of(variant), fairness=fairness)
    return render([judgement]).splitlines()[0]


def test_a_met_expectation_renders_its_evidence_and_fairness() -> None:
    """A pass contributes its state count, then the assumptions it made.

    The state count is what a reader uses to decide whether the exploration was
    large enough to mean anything, so it is a column rather than commentary.
    """
    line = _line_of("holds", "passed", fairness=("F1",))
    summary = render([
        _judge(expectation_of("holds"), result_of("passed"), fairness=("F1",))
    ]).splitlines()[1]

    assert _columns(line) == (
        "head",
        PROPERTY,
        "holds",
        "states 120  fairness: F1",
    ), "every column carries its field"
    assert summary == "model check: 1 expectation met, 0 unmet", "and the tally"
    assert line.index("states") == (
        len("head".ljust(CONFIGURATION_WIDTH))
        + len(_COLUMN_GAP)
        + len(PROPERTY.ljust(PROPERTY_WIDTH))
        + len(_COLUMN_GAP)
        + len("holds".ljust(VERDICT_WIDTH))
        + len(_COLUMN_GAP)
    ), "the evidence begins after the three padded columns"


def test_a_run_with_no_assumption_says_none() -> None:
    """The fairness column is always present, and says ``none`` when empty.

    An omitted column and a run that made no assumption would look the same,
    and the second is a much weaker claim than the first.
    """
    assert _line_of("holds", "passed").endswith("fairness: none"), (
        "an absent assumption is stated"
    )


def test_a_counterexample_renders_its_kind_and_length() -> None:
    """A violation contributes the kind of path and how many steps it took.

    "lasso, 1 step" is what tells a reader whether the defect is a safety
    violation or a liveness one, which is the difference between two different
    repairs; the count also exercises the singular noun.
    """
    columns = _columns(_line_of("fails_as", "counterexample"))

    assert columns[2] == "counterexample as expected", "the verdict"
    assert columns[3].startswith("lasso, 1 step"), "and the path's shape"


def test_a_truncated_run_renders_no_evidence() -> None:
    """A stopped run has no state count and no path, so it contributes neither.

    Printing a count the run never reported would be the report inventing
    evidence for the very outcome that proves nothing.
    """
    assert _columns(_line_of("holds", "stopped"))[3] == "fairness: none", (
        "the verdict runs into fairness"
    )


def test_a_reachable_expectation_names_its_probe() -> None:
    """The property column holds the probe's name for a ``Reachable``.

    A ``Reachable`` expectation has no ``property_name``, so rendering one
    without this branch would raise rather than report.
    """
    columns = _columns(_line_of("reachable", "passed"))

    assert columns[1] == PROBE, "the probe is named"
    assert columns[2] == "reached", "and the verdict is its own"


def test_a_probe_name_longer_than_the_column_is_not_truncated() -> None:
    """A long probe name pushes the later columns right rather than being cut.

    Cutting a name would make two different probes render identically, so a
    reader could not tell which expectation a failure belonged to.
    """
    long_name = "StopIssued"
    judgement = _judge(
        expectation_of("reachable"), result_of("passed"), fairness=(long_name,)
    )

    line = render([judgement]).splitlines()[0]

    assert PROBE in line, "the whole name survives"
    assert line.index("reached") > CONFIGURATION_WIDTH + PROPERTY_WIDTH, (
        "and the verdict moves right"
    )


def test_the_summary_counts_every_judgement() -> None:
    """The summary tallies met and unmet separately, pluralized from the count.

    A report whose summary counted only the matches would make a matrix with
    three failures read as a smaller pass.
    """
    judgements = [
        _judge(expectation_of("holds"), result_of("passed")),
        _judge(expectation_of("holds"), result_of("counterexample")),
        _judge(expectation_of("fails_as"), result_of("counterexample")),
    ]

    summary = render(judgements).splitlines()[-1]

    assert summary == "model check: 2 expectations met, 1 unmet", "the whole tally"


def test_an_empty_matrix_still_renders_a_summary() -> None:
    """No judgements render as the summary alone, not as empty output.

    A tool that printed nothing would be indistinguishable from one that
    crashed before it ran anything.
    """
    assert render([]) == "model check: 0 expectations met, 0 unmet\n", (
        "the summary alone is still a report"
    )


def _drift(
    *,
    unmapped: tuple[str, ...] = (),
    stale: tuple[str, ...] = (),
    excluded: tuple[str, ...] = (),
) -> str:
    """Render a drift report with the given accounts.

    Parameters
    ----------
    unmapped : tuple[str, ...]
        Production paths no model action maps.
    stale : tuple[str, ...]
        Mapped paths that no longer resolve.
    excluded : tuple[str, ...]
        Paths with a recorded reason for not being modelled.

    Returns
    -------
    str
        The rendered report.
    """
    from modelcheck import report
    from modelcheck.drift import DriftReport

    return report.render_drift(DriftReport(unmapped, stale, excluded))


def test_a_clean_drift_report_says_so_in_words() -> None:
    """A comparison with no finding says the model accounts for the code.

    The summary is the line a reader skims, so it states the conclusion rather
    than the count ``0 findings``; the count form would read as a report that
    failed to produce names.
    """
    rendered = _drift()

    assert rendered == (
        "model drift: the model accounts for every production path it names\n"
    ), "a clean comparison is stated as such"


def test_every_drift_account_is_named_and_listed() -> None:
    """Each finding prints under the account it belongs to.

    The three accounts mean different repairs — add a mapping, fix a mapping,
    or record a reason — so a reader has to be able to tell which one a path
    is in. The listing order is the report's, not the caller's.
    """
    rendered = _drift(
        unmapped=("buildlimits.effects.Pause",),
        stale=("buildlimits.recovery_stop.request",),
        excluded=("buildlimits.effects.Exit",),
    )

    assert rendered.splitlines() == [
        "unmapped: buildlimits.effects.Pause",
        "stale: buildlimits.recovery_stop.request",
        "excluded: buildlimits.effects.Exit",
        "model drift: 2 findings",
    ], "every account is listed, and the findings are counted"


def test_exclusions_alone_are_not_findings() -> None:
    """A report whose only content is exclusions still reads as a clean report.

    An exclusion is a decision someone recorded, so counting it as drift would
    make a deliberately scoped model fail the check for ever. The paths still
    print, so a reader can tell a considered gap from a forgotten one.
    """
    rendered = _drift(excluded=("buildlimits.effects.Exit",))

    assert rendered.splitlines()[-1] == (
        "model drift: the model accounts for every production path it names"
    ), "an exclusion is not a finding"
    assert "excluded: buildlimits.effects.Exit" in rendered, "but it is listed"


def test_one_finding_is_counted_in_the_singular() -> None:
    """A single finding reads as ``1 finding``, not ``1 findings``.

    The count is the report's own prose, so it follows the same pluralization
    as every other count the tool prints.
    """
    rendered = _drift(unmapped=("buildlimits.effects.Pause",))

    assert rendered.splitlines()[-1] == "model drift: 1 finding", "one is singular"
