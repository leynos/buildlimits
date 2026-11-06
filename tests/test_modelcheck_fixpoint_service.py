"""The comparison that closes the fixpoint question, and what it leaves behind.

The fixpoint comparison is the documented evidence that FizzBee did not
truncate exploration at ``max_actions``: it prints no marker when it stops, so
the only observable test is that doubling the bound leaves the state count
unchanged. That is why the configurations here declare a bound and why the
double answers from the specification rather than from a recording.

These tests are separate from ``test_modelcheck_service.py`` because they are
about the comparison rather than about the matrix: the matrix judges each
configuration once, and this module asks what happens when a passing one is run
again at twice its bound. The shared model, source and checker doubles live in
``modelcheck_service_cases``.
"""

import pytest
from modelcheck_doubles import FailsDoubled
from modelcheck_service_cases import Bounded, Refusing, bounded_configuration, source

from modelcheck import domain, fixpoint


def test_the_fixpoint_doubles_the_declared_bound() -> None:
    """Twice the declared ``max_actions`` is what the second run is given.

    The bound is read from the configuration's frontmatter rather than being a
    constant, so a configuration that raised its bound is compared against its
    own number rather than the plan's.
    """
    checker = Bounded()

    fixpoint.run_fixpoint(source(bounded_configuration(max_actions=8)), checker)

    assert [text.count("max_actions: 8") for text in checker.spec_texts] == [1, 0], (
        "the declared run keeps its bound"
    )
    assert [text.count("max_actions: 16") for text in checker.spec_texts] == [0, 1], (
        "and the comparison run is given twice it"
    )


def test_a_stable_count_is_reported_as_stable() -> None:
    """An unchanged count at the doubled bound is the fixpoint evidence."""
    found = fixpoint.run_fixpoint(source(bounded_configuration()), Bounded())

    assert found == (fixpoint.Fixpoint("head", 80, 80),), "both counts are reported"
    assert found[0].is_stable, "and the comparison passes"


def test_a_count_that_grows_with_the_bound_is_not_stable() -> None:
    """A run cut short at its bound explores more when the bound is raised."""
    found = fixpoint.run_fixpoint(source(bounded_configuration()), Bounded(grows=True))

    assert found[0].states != found[0].doubled_states, "the growth is visible"
    assert not found[0].is_stable, "and the comparison fails"


def test_a_configuration_without_an_action_bound_cannot_be_compared() -> None:
    """Doubling a bound that is absent would compare two identical runs."""
    with pytest.raises(domain.MaterializeError, match="max_actions"):
        fixpoint.run_fixpoint(
            source(bounded_configuration(max_actions=None)), Bounded()
        )


def test_only_configurations_that_should_pass_are_compared() -> None:
    """A mutant's run stops at its counterexample, so it is not compared.

    Its state count says how far the checker got before the violation, which is
    not a count of the fixed model's states. Asking for a second run of it
    would also risk the doubled bound hiding the very defect it exists to find.
    """
    mutant = bounded_configuration(
        "incident-1",
        expectations=(domain.FailsAs("S1", domain.CounterexampleKind.LASSO),),
    )
    checker = Bounded()

    found = fixpoint.run_fixpoint(source(mutant), checker)

    assert not found, "no comparison is made for a known-bad configuration"
    assert not checker.spec_texts, "and the checker is never asked"


def test_a_run_that_does_not_pass_cannot_be_compared() -> None:
    """A count from a run that proved nothing would compare two errors."""
    with pytest.raises(domain.MaterializeError, match="did not pass"):
        fixpoint.run_fixpoint(source(bounded_configuration()), Refusing())


def test_a_failed_doubled_run_carries_its_own_evidence() -> None:
    """The comparison that failed says which run it was and what it reported.

    The declared run passed, so a sentence naming only the configuration would
    leave a reader unable to tell which of the two runs failed, at what bound,
    or why. The evidence is a value rather than prose so the caller can write it
    into the artefact before the exit status is decided.
    """
    configured = source(bounded_configuration(max_actions=8))

    with pytest.raises(fixpoint.FixpointError) as raised:
        fixpoint.run_fixpoint(configured, FailsDoubled())

    error = raised.value
    assert error.comparisons == (), "no comparison completed"
    (failure,) = error.failures
    assert failure.configuration == "head", "the failed configuration is named"
    assert failure.stage == "doubled", "and the stage that failed"
    assert failure.bound == 16, "with the bound that run was given"
    assert failure.fairness == (), "and the fairness it carried"
    assert isinstance(failure.result, domain.Errored), "its result is kept"
    assert "doubled bound" in failure.result.detail, "including what it said"
    assert "did not pass at its doubled bound" in str(error), (
        "the message names the stage"
    )
