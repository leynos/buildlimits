"""Behavioural scenarios for the model check's verdicts.

Each scenario states one thing a reader of a report must be able to rely on.
The checker double replays a *recorded* output class through the real parser,
so the scenarios pin the translation the tooling will really perform: a broken
parser fails a scenario even though the checker is a double.

The scenarios were written red, against a module-level strict ``xfail``, before
the ports, the service and the exit status existed; that marker is gone now
that all twelve pass, and a scenario that regresses fails rather than being
absorbed by a marker left behind.

The model text, the scenario state and the readers the steps share live in
:mod:`modelcheck_scenarios`, so this module holds only the wording of the steps
and the assertions they make.
"""

import modelcheck_scenarios as scenes
import pytest
from modelcheck_doubles import NEVER_REACHED, FakeChecker, Verbatim
from pytest_bdd import given, parsers, scenarios, then, when

from modelcheck.domain import CounterexampleKind, FailsAs, Holds, Reachable

scenarios("features/model_check.feature")


@pytest.fixture
def matrix() -> scenes.Matrix:
    """Provide a fresh scenario state.

    Returns
    -------
    Matrix
        The state the steps of one scenario build up and read back.
    """
    return scenes.Matrix()


@given("a model check over a model directory")
def _model_directory(matrix: scenes.Matrix) -> None:
    """Leave the matrix empty, ready for a scenario to describe its model."""


@given(parsers.parse('a configuration "{name}" expected to satisfy "{subject}"'))
def _holds(matrix: scenes.Matrix, name: str, subject: str) -> None:
    """Add a fixed configuration that expects the named property to hold."""
    matrix.configurations.append(scenes.declared(name, expectations=(Holds(subject),)))


@given(
    parsers.parse(
        'a configuration "{name}" with a floor of {floor:d} states '
        'expected to satisfy "{subject}"'
    )
)
def _holds_above_floor(
    matrix: scenes.Matrix, name: str, floor: int, subject: str
) -> None:
    """Add a fixed configuration with an exploration floor."""
    matrix.configurations.append(
        scenes.declared(name, min_states=floor, expectations=(Holds(subject),))
    )


@given(
    parsers.parse('a configuration "{name}" expected to fail "{subject}" with a {kind}')
)
def _fails_as(matrix: scenes.Matrix, name: str, subject: str, kind: str) -> None:
    """Add a known-bad configuration expecting a counterexample of a kind."""
    matrix.configurations.append(
        scenes.declared(
            name,
            expectations=(FailsAs(subject, CounterexampleKind[scenes.KINDS[kind]]),),
        )
    )


@given(parsers.parse('a configuration "{name}" expected to reach "{probe}"'))
def _reachable(matrix: scenes.Matrix, name: str, probe: str) -> None:
    """Add a configuration that expects an ``exists`` probe to be satisfied."""
    matrix.configurations.append(
        scenes.declared(name, expectations=(Reachable(probe),))
    )


@given(parsers.parse('the checker output is the fixture "{output_class}"'))
def _fixture(matrix: scenes.Matrix, output_class: str) -> None:
    """Answer every run with the named recorded output class."""
    matrix.checker = FakeChecker(
        output_class, property_name=scenes.expected_failure(matrix)
    )


@given(
    parsers.parse(
        'the checker output is the fixture "{output_class}" '
        "with {states:d} states explored"
    )
)
def _fixture_states(matrix: scenes.Matrix, output_class: str, states: int) -> None:
    """Answer with a recorded class over a stated number of states.

    The recorded ``pass`` class explored four states, so against a floor of
    1000 it would be below it for the fixture's reason, not the scenario's.
    """
    matrix.checker = FakeChecker(output_class)
    matrix.states_override = states


@given("the checker output has no verdict line")
def _no_verdict(matrix: scenes.Matrix) -> None:
    """Answer with output carrying no verdict at all."""
    matrix.checker = Verbatim("Model checking example.json\n")


@given(parsers.parse('the checker reports that "{probe}" was never satisfied'))
def _never_satisfied(matrix: scenes.Matrix, probe: str) -> None:
    """Answer with the never-reached completion, naming one probe."""
    matrix.checker = Verbatim(NEVER_REACHED.format(probe=probe))


@given(
    parsers.parse(
        'the model tags fairness "{tags}" on the actions "{subject}" relies on'
    )
)
def _fairness(matrix: scenes.Matrix, tags: str, subject: str) -> None:
    """Tag one action per fairness tag, so a run can carry all of them.

    ``materialize`` matches each tag a configuration names against a
    ``# fairness:`` annotation on an action line and raises when one is absent,
    so the tags cannot share a line: each needs its own action. The tagged
    actions are appended to the shared source rather than replacing it: the
    property the scenario names still has to be declared, because the
    materializer refuses an expectation the model does not carry.
    """
    del subject
    names = tags.split(", ")
    actions = "\n\n".join(
        f"atomic fair action Step{index}:  # fairness: {tag}\n  n = n + 1"
        for index, tag in enumerate(names, start=1)
    )
    matrix.source = f"{scenes.DEFAULT_SOURCE}\n{actions}\n"
    matrix.fairness = dict.fromkeys(names, "weak")


@given("the model maps every admission event and effect")
def _maps_everything(matrix: scenes.Matrix) -> None:
    """Start from a model whose mappings cover the whole inventory."""
    from modelcheck import drift

    matrix.source = scenes.DEFAULT_SOURCE
    matrix.mapped = drift.parse_mappings(scenes.DEFAULT_SOURCE)
    matrix.inventory = matrix.mapped
    matrix.resolves = dict.fromkeys(matrix.mapped, True)


@given(parsers.parse('the model maps "{path}"'))
def _maps(matrix: scenes.Matrix, path: str) -> None:
    """Start from a model that maps exactly one path."""
    matrix.source = f"action Init:  # maps: {path}\n  n = 0\n"
    matrix.mapped = frozenset({path})
    matrix.inventory = frozenset({path})
    matrix.resolves = {path: True}


@given(parsers.parse('the code gains an effect "{path}"'))
def _gained(matrix: scenes.Matrix, path: str) -> None:
    """Add an inventory item the model does not map."""
    matrix.inventory = frozenset({*matrix.inventory, scenes.qualified(path)})


@given("that path no longer resolves")
def _renamed(matrix: scenes.Matrix) -> None:
    """Make every mapped path fail to resolve."""
    matrix.resolves = dict.fromkeys(matrix.mapped, False)


@when("the model check runs")
def _runs(matrix: scenes.Matrix) -> None:
    """Run the matrix and judge every expectation."""
    from modelcheck_doubles import Restated

    from modelcheck import service

    if matrix.states_override is not None:
        matrix.checker = Restated(matrix.checker, matrix.states_override)
    matrix.judgements = service.run_matrix(scenes.reconfigured(matrix), matrix.checker)


@when("the drift check runs")
def _drift_runs(matrix: scenes.Matrix) -> None:
    """Compare the model's mappings with the inventory."""
    from modelcheck import drift

    matrix.drift_report = drift.drift(
        matrix.mapped,
        matrix.inventory,
        lambda path: matrix.resolves.get(path, False),
        {},
    )


@then(parsers.parse('the report shows "{configuration}" "{subject}" as "{verdict}"'))
def _shows(
    matrix: scenes.Matrix, configuration: str, subject: str, verdict: str
) -> None:
    """Check the rendered report carries the row a reader would look for."""
    line = scenes.row(matrix, configuration, subject)

    assert verdict in line, f"{verdict!r} not in {line!r}"


@then(parsers.parse("the model check exits with status {status:d}"))
def _exits(matrix: scenes.Matrix, status: int) -> None:
    """Check the exit status the command-line interface would return."""
    from modelcheck import cli, report

    assert cli.exit_status(matrix.judgements) == status, report.render_report(
        matrix.judgements
    )


@then(parsers.parse('the report line for "{configuration}" "{subject}" names "{tags}"'))
def _names_fairness(
    matrix: scenes.Matrix, configuration: str, subject: str, tags: str
) -> None:
    """Check the row quotes the fairness the run carried."""
    line = scenes.row(matrix, configuration, subject)

    for tag in tags.split(", "):
        assert tag in line, f"{tag!r} missing from {line!r}"


@then(parsers.parse('it fails naming "{path}"'))
def _names(matrix: scenes.Matrix, path: str) -> None:
    """Check the drift report is not clean and names the path."""
    assert matrix.drift_report is not None, "the drift check ran"
    assert not matrix.drift_report.is_clean, f"{matrix.drift_report} is clean"
    named = {*matrix.drift_report.unmapped, *matrix.drift_report.stale}
    assert path in named, f"{path!r} not among {sorted(named)}"
