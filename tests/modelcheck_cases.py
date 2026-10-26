"""Values the ``modelcheck`` domain tests build, named once.

Three test modules share the same small vocabulary: an expectation of each
kind, a result of each variant, a configuration that expects one thing, a
materialized specification, and a normalized trace. Writing them out in each
module would make four copies drift apart silently, and a change to a
constructor would have to be made in all of them.

This module is test support and is linted as production code, following the
convention in ``docs/repository-layout.md``: ``test_*.py`` modules hold tests,
anything else here holds helpers.
"""

import typing as typ

from hypothesis import strategies as st

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck.domain import (
        CheckResult,
        Configuration,
        Expectation,
        Judgement,
        MaterializedSpec,
    )
    from modelcheck.trace import Trace

FIXED = """\
action Init:
  n = 0

atomic fair action Bump:      # fairness: F1
  n = n + 1

always eventually assertion ReachesTen:
  return n >= 10
"""
"""A model exercising a fairness rewrite and one liveness assertion."""

TAGGED = """\
---
options:
  max_actions: 4
---

action Init:                 # fairness: F2
  n = 0

atomic fair<strong> action Bump:  # fairness: F1
  n = n + 1

always assertion UnderTen:
  return n < 10

always eventually assertion ReachesTen:
  return n >= 10

exists assertion ReachesOne:
  return n == 1
"""
"""A model with frontmatter, two tagged actions and three assertions."""

IDENTIFIER = st.from_regex(r"[A-Z][A-Za-z0-9]{0,6}", fullmatch=True)
"""A name shaped like a model constant, an action or a fairness tag."""

LEVEL = st.sampled_from(["weak", "strong", "unfair"])
"""The three fairness levels a configuration may name."""

PROBE = "StopIssued"
"""The probe name the expectation tests use."""

PROPERTY = "L3"
"""The property name the expectation tests use."""


def expectation_of(kind: str) -> Expectation:
    """Build one expectation of the named kind.

    Parameters
    ----------
    kind : str
        One of ``holds``, ``fails_as`` or ``reachable``.

    Returns
    -------
    Expectation
        The expectation the kind names, over :data:`PROPERTY` or
        :data:`PROBE`.
    """
    from modelcheck import domain

    builders = {
        "holds": lambda: domain.Holds(PROPERTY),
        "fails_as": lambda: domain.FailsAs(PROPERTY, domain.CounterexampleKind.LASSO),
        "reachable": lambda: domain.Reachable(PROBE),
    }
    return builders[kind]()


def result_of(variant: str, *, probe_satisfied: bool = True) -> CheckResult:
    """Build one result of the named variant.

    Parameters
    ----------
    variant : str
        One of ``passed``, ``counterexample``, ``stopped`` or ``errored``.
    probe_satisfied : bool
        Whether a pass reports :data:`PROBE` as satisfied.

    Returns
    -------
    CheckResult
        The result the variant names.
    """
    from modelcheck import domain

    if variant == "passed":
        probes = () if probe_satisfied else (PROBE,)
        return domain.Passed(120, 0.5, probes)
    if variant == "counterexample":
        return domain.Counterexample(
            PROPERTY, domain.CounterexampleKind.LASSO, trace(), 9, 0.2
        )
    if variant == "stopped":
        return domain.Stopped("max_actions 6 reached")
    return domain.Errored("no verdict line")


# One keyword per field of the value it builds, so a call site names exactly the
# fields its case is about. Grouping them into a parts object would move the
# same names one level down and lengthen every call site. The arity is the
# builder's interface, not an accident, so both limits are waived permanently.
def configuration(  # ruff: ignore[too-many-arguments]  # one keyword per field, permanent; pylint: disable=too-many-arguments
    name: str,
    *,
    constants: dict[str, str] | None = None,
    fairness: dict[str, str] | None = None,
    options: dict[str, object] | None = None,
    min_states: int = 1,
    expectations: tuple[Expectation, ...] = (),
) -> Configuration:
    """Build a configuration, defaulting every part to "changes nothing".

    Parameters
    ----------
    name : str
        The configuration's name.
    constants : dict[str, str] | None
        Top-level constants to substitute.
    fairness : dict[str, str] | None
        Fairness tag to level.
    options : dict[str, object] | None
        The complete frontmatter.
    min_states : int
        The exploration floor.
    expectations : tuple[Expectation, ...]
        What the configuration expects.

    Returns
    -------
    Configuration
        The configuration those parts describe.
    """
    from modelcheck import domain

    return domain.Configuration(
        name=name,
        constants=constants or {},
        fairness=fairness or {},
        options=options or {},
        min_states=min_states,
        expectations=expectations,
    )


def spec(
    configuration_name: str = "head",
    *,
    text: str = "",
    fairness: tuple[str, ...] = ("F1", "F2"),
) -> MaterializedSpec:
    """Build a materialized specification for the expectation tests.

    Parameters
    ----------
    configuration_name : str
        The configuration the text was produced for.
    text : str
        The specification text.
    fairness : tuple[str, ...]
        The fairness tags the run carries.

    Returns
    -------
    MaterializedSpec
        The specification.
    """
    from modelcheck import domain

    return domain.MaterializedSpec(configuration_name, text, fairness)


def render(judgements: cabc.Sequence[Judgement]) -> str:
    """Render judgements through the module under test.

    Parameters
    ----------
    judgements : collections.abc.Sequence[Judgement]
        The judgements to render.

    Returns
    -------
    str
        The rendered report, exactly as a maintainer would see it.
    """
    from modelcheck import report

    return report.render_report(judgements)


def trace() -> Trace:
    """Build a one-step lasso for a counterexample to carry.

    The trace's content is irrelevant to the judgement table;
    ``test_modelcheck_trace`` is what pins the normalization. Building a real
    value keeps every counterexample row a real result.

    Returns
    -------
    Trace
        A lasso whose cycle closes onto its only state.
    """
    from modelcheck.trace import Ending, Step
    from modelcheck.trace import Trace as NormalizedTrace

    return NormalizedTrace({"b": 0}, (Step("Gate", {"b": 0}),), Ending.LASSO, 0)
