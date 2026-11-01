"""The W1 models' matrices, judged from the runs a maintainer just made.

This module does not invoke the checker. It reads the artefacts ``make
model-check`` writes, ``.model-check/coupling.json`` and
``.model-check/transaction.json``, and judges them against the milestone's
acceptance criteria: every property each fixed configuration claims holds,
every probe it claims is reachable, and every targeted mutant still producing
the counterexample it was built to produce.

The two artefacts belong to the two halves H2 split the unsplit model into. A
property is carried by exactly one half, except S2, whose token accounting
each half restates over its own state. Judging the halves together is the
point: a split model that quietly dropped a property would leave a matrix that
is entirely green.

Keeping the run and the judgement apart is deliberate. The checker run is
expensive and its bounds are the plan's, so the Makefile target owns it; the
judgement is cheap and belongs in the ordinary test run. A test that ran the
checker itself would either duplicate the recipe or quietly use different
bounds from the report, and a mutant that stopped failing would then be
invisible to the target that people actually run.

Each results file must be newer than the model it describes. A stale artefact
is the one way this module could pass while the model no longer deserves it,
and nothing in the checker's own output can reveal that, so the age is checked
here with the rest of the acceptance criteria.
"""

import json
import typing as typ
from pathlib import Path

import pytest

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
    "coupling": {"head-no-cached-guard": "S1"},
    "transaction": {
        "head-no-stale-proof": "L1",
        "head-no-grace-deadline": "L1",
        "head-unknown-roots": "L1",
        "head-unfair-leader-exits": "L3",
    },
}
"""Each mutant and the property it must still break."""

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
    An absent or stale artefact fails the assertion rather than skipping the
    test. Either means the matrix was not run against the source under test,
    and a skip would report that gap as a pass.
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
    """Provide both models' matrix records once for the module.

    Returns
    -------
    dict[str, list[dict[str, object]]]
        The records, keyed by model name.
    """
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


@pytest.mark.parametrize("model", sorted(_MODELS))
def test_the_matrix_names_every_configuration(
    records: dict[str, list[Record]], model: str
) -> None:
    """Every configuration the model requires appears, and no other does.

    A mutant that vanished from the matrix would otherwise pass silently: the
    remaining records would all be met, and nothing would be reported about
    the property it was built to break.
    """
    reported = sorted({str(record["configuration"]) for record in records[model]})

    assert reported == sorted({"head", *MUTANTS[model]}), (
        f"the {model} matrix ran {reported}, not its fixed and mutant configurations"
    )


@pytest.mark.parametrize("model", sorted(_MODELS))
def test_fixed_configuration_holds(
    records: dict[str, list[Record]], model: str
) -> None:
    """``head`` meets every expectation, and says which fairness it assumes.

    The fairness assertion is not decoration. A liveness pass is only a claim
    about the paths fairness admits, so a report line that could not name the
    tags in force would not be evidence for the property it claimed.
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
    asserting that the one failing subject is the predicted one also asserts
    the kind: a wrong kind is reported as ``wrong counterexample kind`` and
    would not be met.
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
