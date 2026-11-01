"""The W1 models' matrices, judged from the runs a maintainer just made.

This module does not invoke the checker. It reads the artefacts ``make
model-check`` writes, ``.model-check/coupling.json`` and
``.model-check/transaction.json``, and judges them against the milestone's
acceptance criteria: every property each fixed configuration claims holds,
every probe it claims is reachable, and every targeted mutant still producing
the counterexample it was built to produce. A property is carried by exactly
one half, except S2, whose token accounting each half restates over its own
state; judging the halves together is the point, because a split model that
quietly dropped a property would leave a matrix that is entirely green.

Keeping the run and the judgement apart is deliberate: the checker run is
expensive and its bounds are the plan's, so the Makefile target owns it, while
the judgement is cheap and belongs in the ordinary test run. Each results file
must also be newer than the model it describes, because a stale artefact is the
one way this module could pass while the model no longer deserves it.

Two obligations are checked against the real models rather than a test double.
Every name each configuration expects must still be declared by the model it
names, because the materializer is the only place that refusal can happen. And
every mutant must report the fairness its run still carries, because the report
quotes the run rather than the configuration. Materializing is not invoking the
checker: it rewrites text already in the tree.
"""

import json
import typing as typ
from pathlib import Path

import pytest

from modelcheck import domain, materialize
from modelcheck.adapters.configuration import TomlConfigurations

_ROOT = Path(__file__).resolve().parents[1]
"""The checkout root, which is where ``make model-check`` writes its results."""

_MODELS = {
    "coupling": _ROOT / "models" / "admission" / "coupling",
    "transaction": _ROOT / "models" / "admission" / "transaction",
}
"""The two W1 model directories, each the source its artefact must postdate."""

pytestmark = [pytest.mark.fizzbee]

PROPERTIES = {
    "coupling": ("S1", "S2", "L4a"),
    "transaction": ("S2", "L1", "L3", "L4b"),
}
"""The properties each model claims on ``head``, in declaration order."""

PROBES = {
    "coupling": (
        "GroupRunning",
        "GroupDraining",
        "CachedAndUncachedRunTogether",
        "StopTakenProbe",
        "RefusalTaken",
    ),
    "transaction": (
        "StopIssued",
        "TokenReleasedByAccounting",
        "ReachesRestarting",
        "GroupServerDrain",
    ),
}
"""The non-vacuity probes each model requires ``head`` to satisfy."""

MUTANTS = {
    "coupling": {
        "head-no-cached-guard": "S1",
        "head-no-manager-guard": "S1",
    },
    "transaction": {
        "head-no-stale-proof": "L1",
        "head-no-grace-deadline": "L1",
        "head-unknown-roots": "L1",
        "head-no-accounting-release": "L1",
        "head-unfair-leader-exits": "L3",
    },
}
"""Each mutant and the property it must still break."""

CONTROLS = {
    "coupling": {},
    "transaction": {
        "head-retention-idle-queue": "L1",
    },
}
"""Each control and the property it must show still holds.

A control disables the same mechanism a mutant does, but in an environment
where the contract permits the result. ``head-retention-idle-queue`` keeps the
token retained for ever while the queue empties between requests, which RFC
0001 RC5 says is not a starvation failure, so L1 must hold. Without it the
demand qualification would be prose with no run behind it.
"""

type Record = dict[str, object]


def _records(model: str) -> list[Record]:
    """Return one model's matrix artefact records.

    Parameters
    ----------
    model : str
        The model directory's name, ``coupling`` or ``transaction``.

    Returns
    -------
    list[dict[str, object]]
        The decoded array ``modelcheck check --json`` wrote for that model.

    Notes
    -----
    An absent or stale artefact fails rather than skips: a skip would report a
    matrix that was not run against the source under test as a pass.
    """
    results = _ROOT / ".model-check" / f"{model}.json"
    assert results.exists(), (
        f"{results} is missing; run `make model-check` before this suite"
    )
    newest_source = max(path.stat().st_mtime for path in _MODELS[model].rglob("*"))
    assert results.stat().st_mtime >= newest_source, (
        f"{results} is older than the model; run `make model-check` again"
    )
    return typ.cast("list[Record]", json.loads(results.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def records() -> dict[str, list[Record]]:
    """Provide both models' matrix records once, keyed by model name."""
    return {model: _records(model) for model in _MODELS}


def _of(records: list[Record], configuration: str) -> dict[str, Record]:
    """Return one configuration's records keyed by subject.

    Parameters
    ----------
    records : list[dict[str, object]]
        One model's whole matrix.
    configuration : str
        The configuration to select.

    Returns
    -------
    dict[str, dict[str, object]]
        Its records, keyed by the property or probe they judge.
    """
    return {
        typ.cast("str", record["subject"]): record
        for record in records
        if record["configuration"] == configuration
    }


def _expected_failures(configuration: domain.Configuration) -> tuple[str | None, ...]:
    """Return the runs one configuration's matrix entry materializes.

    The service builds this set itself, and a materializer test that answered a
    different set would prove the names of runs nobody makes. When every
    expectation must fail there is no passing run; otherwise one run passes,
    plus one per predicted failure.

    Parameters
    ----------
    configuration : modelcheck.domain.Configuration
        The variant to read.

    Returns
    -------
    tuple[str | None, ...]
        The expected failure of each run, in the service's order.
    """
    failures = tuple(
        expectation.property_name
        for expectation in configuration.expectations
        if isinstance(expectation, domain.FailsAs)
    )
    if len(failures) == len(configuration.expectations):
        return failures
    return (None, *failures)


@pytest.mark.parametrize("model", sorted(_MODELS))
def test_every_expectation_is_declared_by_the_model(model: str) -> None:
    """Every name a configuration expects is one the real model declares.

    The materializer refuses a name the source does not carry, which is what
    stops a property that vanished from being reported as holding. This
    materializes every run the matrix would ask for, so a name that drifted out
    of the model fails here instead of passing as a green row.
    """
    source = TomlConfigurations(_MODELS[model])
    text = source.source_text()

    for configuration in source.configurations():
        assert configuration.expectations, (
            f"{model} {configuration.name} expects nothing, so no run could judge it"
        )
        for expected_failure in _expected_failures(configuration):
            spec = materialize.materialize(
                text, configuration, expected_failure=expected_failure
            )
            assert spec.configuration == configuration.name, (
                "each run names the configuration it was materialized for"
            )


@pytest.mark.parametrize(
    ("declaration", "renamed", "absent"),
    [
        (
            "always eventually assertion L4b:",
            "always eventually assertion L4bElsewhere:",
            "L4b",
        ),
        (
            "exists assertion ReachesRestarting:",
            "exists assertion ReachesRestartingElsewhere:",
            "ReachesRestarting",
        ),
    ],
    ids=["held-property", "probe"],
)
def test_a_lost_declaration_is_refused_before_the_checker_runs(
    declaration: str, renamed: str, absent: str
) -> None:
    """Controls: losing a held property or a probe refuses the whole run.

    Renaming is the smallest edit that makes the expectation dangle, so a
    refusal here can only be the missing name, and the matching assertion
    proves the refusal names the expectation rather than another defect.
    """
    source = TomlConfigurations(_MODELS["transaction"])
    text = source.source_text()
    head = next(
        configuration
        for configuration in source.configurations()
        if configuration.name == "head"
    )
    assert declaration in text, (
        "the control renames a declaration the model still carries"
    )

    with pytest.raises(domain.MaterializeError, match=absent):
        materialize.materialize(
            text.replace(declaration, renamed), head, expected_failure=None
        )


@pytest.mark.parametrize("model", sorted(_MODELS))
def test_each_mutant_reports_the_fairness_it_still_carries(
    records: dict[str, list[Record]], model: str
) -> None:
    """A mutant's rows name every assumption it kept and none it disabled.

    The expected set is read from the fixed run, which names every tag, less
    the tags the mutant turns unfair: a mutant naming no fairness table keeps
    the model's own annotations, so a report built from the configuration would
    claim the run assumed nothing while its actions were still fair. The
    artefact is judged too, because the report is what a reader acts on.
    """
    source = TomlConfigurations(_MODELS[model])
    text = source.source_text()
    fixed = next(
        configuration
        for configuration in source.configurations()
        if configuration.name == "head"
    )
    carried = materialize.materialize(
        text, fixed, expected_failure=None
    ).fairness_in_force
    assert carried, f"{model} head carries no fairness, so nothing could be reported"

    for configuration in source.configurations():
        if configuration.name == "head":
            continue
        disabled = {
            tag for tag, level in configuration.fairness.items() if level == "unfair"
        }
        expected = tuple(tag for tag in carried if tag not in disabled)
        assert expected, (
            f"{configuration.name} disables every fairness tag, so its report "
            "would name no assumption at all"
        )
        spec = materialize.materialize(
            text, configuration, expected_failure=_expected_failures(configuration)[0]
        )
        assert spec.fairness_in_force == expected, (
            f"{configuration.name} carries {spec.fairness_in_force}, not {expected}"
        )
        rows = _of(records[model], configuration.name)
        assert rows, f"{configuration.name} produced no rows to judge"
        reported = {
            tuple(typ.cast("list[str]", record["fairness"])) for record in rows.values()
        }
        assert reported == {expected}, (
            f"{configuration.name} reported {sorted(reported)}, not {expected}"
        )


@pytest.mark.parametrize("model", sorted(_MODELS))
def test_the_matrix_names_every_configuration(
    records: dict[str, list[Record]], model: str
) -> None:
    """Every configuration the model requires appears, and no other does.

    A mutant that vanished from the matrix would otherwise pass silently: the
    remaining records would all be met, and nothing would report the property
    it was built to break.
    """
    reported = sorted({str(record["configuration"]) for record in records[model]})

    assert reported == sorted({"head", *MUTANTS[model], *CONTROLS[model]}), (
        f"the {model} matrix ran {reported}, not its fixed, mutant and control "
        "configurations"
    )


@pytest.mark.parametrize("model", sorted(_MODELS))
def test_fixed_configuration_holds(
    records: dict[str, list[Record]], model: str
) -> None:
    """``head`` meets every expectation, and says which fairness it assumes.

    The fairness assertion is not decoration: a liveness pass is a claim only
    about the paths fairness admits, so a line that could not name the tags in
    force would not be evidence for the property it claimed.
    """
    head = _of(records[model], "head")
    assert set(head) == {*PROPERTIES[model], *PROBES[model]}, (
        f"{model} head reported {sorted(head)}, "
        f"not {PROPERTIES[model]} and {PROBES[model]}"
    )
    for subject in PROPERTIES[model]:
        record = head[subject]
        assert record["met"] is True, record
        assert record["verdict"] == "holds", record
        assert record["fairness"], (
            f"{model} {subject} passed without naming the fairness it assumes"
        )
    for subject in PROBES[model]:
        record = head[subject]
        assert record["met"] is True, record
        assert record["verdict"] == "reached", record


@pytest.mark.parametrize("model", sorted(_MODELS))
def test_mutants_fail(records: dict[str, list[Record]], model: str) -> None:
    """Each targeted mutant produces exactly the counterexample it predicts.

    The expected kind (a lasso, or a finite path for the seeded coupling fault)
    is checked by ``modelcheck`` against the configuration's ``fails_as``, so
    asserting the one failing subject is the predicted one also asserts the
    kind: a wrong kind is reported as ``wrong counterexample kind``.
    """
    for configuration, broken in MUTANTS[model].items():
        mutant = _of(records[model], configuration)
        assert all(record["met"] is True for record in mutant.values()), mutant
        failing = [
            subject
            for subject, record in mutant.items()
            if record["verdict"] == "counterexample as expected"
        ]
        assert failing == [broken], (
            f"{configuration} should fail only {broken}, not {failing}"
        )


@pytest.mark.parametrize("model", sorted(_MODELS))
def test_controls_hold(records: dict[str, list[Record]], model: str) -> None:
    """Each control meets the property it is evidence for.

    A control fails the way a claim fails when nobody writes it down: the
    constants could be lost, or the property weakened until it holds for the
    wrong reason, and the matrix would still be green. Naming the subject here
    makes a mistaken control a failing run rather than an extra passing row.
    """
    for configuration, subject in CONTROLS[model].items():
        rows = _of(records[model], configuration)
        assert set(rows) == {subject}, (
            f"{configuration} reported {sorted(rows)}, not only {subject}"
        )
        record = rows[subject]
        assert record["met"] is True, record
        assert record["verdict"] == "holds", record
