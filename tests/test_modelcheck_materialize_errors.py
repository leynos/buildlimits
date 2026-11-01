"""A configuration that names something absent is refused, never ignored.

Every refusal here guards the same failure: a variant that silently ran the
fixed model while its report claimed otherwise. A misspelt constant would run
the fixed value, a misspelt fairness tag would leave the fixed level on, an
unknown level would reach the checker as a syntax error in a file the tool
wrote, and a retained assertion that names nothing would leave every property
in the run so the checker could fail on any of them.

The materializer is also the last place that can notice a run expecting a
failure belonging to a different configuration, or a configuration expecting a
failure that the run was not given. D3 gives each run exactly one expected
failure, so both are defects of the caller rather than of the model.

Every test here was written before its module was implemented (EP-M2), so it
pins a contract the stub could not satisfy.
"""

import modelcheck_cases as cases
import pytest

from modelcheck import domain, materialize

_UNKNOWN_NAMES = [
    pytest.param(
        {"constants": {"CAPACITTY": "2"}},
        "CAPACITTY",
        id="unknown-constant",
    ),
    pytest.param({"fairness": {"F9": "weak"}}, "F9", id="unknown-fairness-tag"),
    pytest.param(
        {"fairness": {"F1": "sometimes"}},
        "sometimes",
        id="unknown-fairness-level",
    ),
]
"""The three names a configuration may give that the model does not carry.

Each is the same defect at a different layer: a name the configuration
believes exists and the source does not. Silently ignoring one would run the
fixed model while the report claimed otherwise (D4), or leave the report
naming an assumption no action carries.
"""


def _with_name(fields: dict[str, dict[str, str]]) -> cases.Configuration:
    """Build the configuration one named field implies.

    The builder takes one keyword per field, so the parametrizations cannot be
    splatted directly: a splat of a heterogeneous mapping is checked against
    every keyword at once. Each case names exactly one of the builder's four
    mapping keywords, so the mapping is narrowed here where its shape is known.

    Parameters
    ----------
    fields : dict[str, dict[str, str]]
        One of the builder's mapping keywords and the value to give it.

    Returns
    -------
    cases.Configuration
        The configuration for the ``typo`` variant.
    """
    if set(fields) == {"constants"}:
        return cases.configuration("typo", constants=fields["constants"])
    return cases.configuration("typo", fairness=fields["fairness"])


def _refused(
    text: str,
    configuration: cases.Configuration,
    *,
    expected_failure: str | None,
    named: str,
) -> None:
    """Assert this run is refused, and that the message names the defect.

    Every refusal below is the same contract at a different layer, so the
    assertion that carries it lives here rather than being restated beside each
    one. A match is part of the contract: the caller's next step is to correct
    the name the message reports, so a refusal that said something else would
    send them hunting through the model instead.

    Parameters
    ----------
    text : str
        The model source to materialize.
    configuration : cases.Configuration
        The configuration whose run is expected to be refused.
    expected_failure : str | None
        The single failure the run should be given, if any.
    named : str
        The text the refusal must name, as a regular expression.
    """
    with pytest.raises(materialize.MaterializeError, match=named):
        materialize.materialize(text, configuration, expected_failure=expected_failure)


@pytest.mark.parametrize(("fields", "named"), _UNKNOWN_NAMES)
def test_a_name_the_model_does_not_carry_is_an_error(
    fields: dict[str, dict[str, str]], named: str
) -> None:
    """A constant, fairness tag or level the source cannot supply must raise.

    The raised message names what could not be found, because the caller's next
    step is to correct that name rather than to hunt through the model.
    """
    _refused(cases.FIXED, _with_name(fields), expected_failure=None, named=named)


def test_a_retained_assertion_must_exist() -> None:
    """A declared expectation the source does not declare must raise.

    This is the rename case, and it is the quietest defect the materializer can
    be handed: the configuration still declares ``ReachesTen``, the model has
    been renamed to ``ReachesTenV2``, so stripping leaves a specification that
    asserts nothing at all. The checker would then report a pass, and the
    report would read as the expectation having been met.

    The configuration declares the name, so this is not the declaration check
    firing by accident; only a search of the source can catch it.
    """
    configuration = cases.configuration(
        "typo",
        expectations=(domain.FailsAs("ReachesTen", domain.CounterexampleKind.LASSO),),
    )
    renamed = cases.FIXED.replace("ReachesTen", "ReachesTenV2")

    _refused(
        renamed,
        configuration,
        expected_failure="ReachesTen",
        named="ReachesTen",
    )


@pytest.mark.parametrize(
    ("expectation", "declaration", "renamed", "named"),
    [
        pytest.param(
            domain.Holds("UnderTen"),
            "assertion UnderTen:",
            "assertion UnderTenV2:",
            "UnderTen",
            id="held-property",
        ),
        pytest.param(
            domain.Reachable("ReachesOne"),
            "assertion ReachesOne:",
            "assertion ReachesOneV2:",
            "ReachesOne",
            id="probe",
        ),
    ],
)
def test_an_expectation_naming_a_renamed_declaration_is_an_error(
    expectation: domain.Expectation, declaration: str, renamed: str, named: str
) -> None:
    """A ``Holds`` or ``Reachable`` over a renamed declaration must raise.

    Both are the fail-open path that only a source lookup closes. A ``Holds``
    expectation keeps its assertion rather than stripping it, so an earlier
    version of the materializer never looked the name up. A ``Reachable`` is
    judged from the unsatisfied-probe list a passing run reports, so a probe
    the model never declares is simply absent from that list and the judgement
    reads it as reached. Either way the checker compiles the specification,
    finds nothing to complain about, and the report reads as the expectation
    having been met. The unrenamed source is the negative control that proves
    the refusal is about the missing name and not about the configuration
    being unusable.
    """
    configuration = cases.configuration("head", expectations=(expectation,))
    materialize.materialize(cases.TAGGED, configuration, expected_failure=None)

    _refused(
        cases.TAGGED.replace(declaration, renamed),
        configuration,
        expected_failure=None,
        named=named,
    )


def test_an_expected_failure_must_be_one_of_the_configurations_own() -> None:
    """A run may only expect a failure its configuration declares.

    Retaining an assertion the configuration never mentions would make the run
    about a property the report does not name, so the materializer checks the
    two against each other.
    """
    configuration = cases.configuration(
        "head", expectations=(domain.Holds("ReachesTen"),)
    )

    _refused(
        cases.FIXED,
        configuration,
        expected_failure="ReachesTen",
        named="ReachesTen",
    )


def test_a_configuration_expecting_a_failure_must_be_given_one() -> None:
    """A ``FailsAs`` expectation without a run of its own must raise.

    D3 gives each run exactly one expected failure, so a configuration that
    declares one but runs with ``expected_failure=None`` would check every
    property at once and could fail on a different one.
    """
    configuration = cases.configuration(
        "incident-1",
        expectations=(domain.FailsAs("ReachesTen", domain.CounterexampleKind.LASSO),),
    )

    _refused(
        cases.FIXED,
        configuration,
        expected_failure=None,
        named="expected failure",
    )
