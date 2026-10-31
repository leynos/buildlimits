"""The admission model's matrix, judged from the run a maintainer just made.

This module does not invoke the checker. It reads the artefact ``make
model-check`` writes, ``.model-check/results.json``, and judges it against the
milestone's acceptance criteria: every property the fixed configuration claims
holds, every probe it claims is reachable, and every targeted mutant still
producing the counterexample it was built to produce.

Keeping the two apart is deliberate. The checker run is expensive and its
bounds are the plan's, so the Makefile target owns it; the judgement is cheap
and belongs in the ordinary test run. A test that ran the checker itself would
either duplicate the recipe or quietly use different bounds from the report,
and a mutant that stopped failing would then be invisible to the target that
people actually run.

The results file must be newer than the model it describes. A stale artefact
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

_MODEL = _ROOT / "models" / "admission"
"""The model directory whose newest source the results must postdate."""

_RESULTS = _ROOT / ".model-check" / "results.json"
"""The matrix artefact the ``model-check`` target writes."""

pytestmark = [pytest.mark.fizzbee]

_MET = frozenset({"holds", "counterexample as expected", "reached"})
"""The verdicts that satisfy their expectation, as ``modelcheck`` defines them."""

PROPERTIES = ("S1", "S2", "L1", "L3", "L4a", "L4b")
"""The six properties W1 claims on ``head``, in declaration order."""

PROBES = (
    "StopIssued",
    "TokenReleasedByAccounting",
    "ReachesRestarting",
    "GroupRunning",
    "GroupDraining",
    "GroupServerDrain",
    "CachedAndUncachedRunTogether",
)
"""The seven non-vacuity probes W1 requires ``head`` to satisfy."""

LASSOS = {
    "head-no-stale-proof": "L1",
    "head-no-grace-deadline": "L1",
    "head-unknown-roots": "L1",
    "head-unfair-leader-exits": "L3",
}
"""The mutants that must still produce a lasso, and the property it breaks."""

SEEDED = "head-no-cached-guard"
"""The configuration that deletes the ``cached_moving`` guard, breaking S1."""

type Record = dict[str, object]


def _records() -> list[Record]:
    """Return the matrix artefact's records.

    Returns
    -------
    list[dict[str, object]]
        The decoded array ``modelcheck check --json`` writes.

    Notes
    -----
    An absent or stale artefact fails the assertion rather than skipping the
    test. Either means the matrix was not run against the source under test,
    and a skip would report that gap as a pass.
    """
    assert _RESULTS.exists(), (
        f"{_RESULTS} is missing; run `make model-check` before this suite"
    )
    newest_source = max(path.stat().st_mtime for path in _MODEL.rglob("*"))
    assert _RESULTS.stat().st_mtime >= newest_source, (
        f"{_RESULTS} is older than the model; run `make model-check` again"
    )
    return typ.cast("list[Record]", json.loads(_RESULTS.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def records() -> list[Record]:
    """Provide the matrix artefact's records once for the module.

    Returns
    -------
    list[dict[str, object]]
        The records, each naming its configuration and subject.
    """
    return _records()


def _of(records: list[Record], configuration: str) -> dict[str, Record]:
    """Return one configuration's records keyed by subject.

    Parameters
    ----------
    records : list[dict[str, object]]
        The whole matrix.
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


def test_the_matrix_names_every_configuration(records: list[Record]) -> None:
    """Every configuration W1 requires appears, and no other does.

    A mutant that vanished from the matrix would otherwise pass silently: the
    remaining records would all be met, and nothing would be reported about
    the property it was built to break.
    """
    reported = sorted(str(record["configuration"]) for record in records)

    assert reported == sorted({"head", *LASSOS, SEEDED}), (
        f"the matrix ran {reported}, not the six W1 configurations"
    )


def test_fixed_configuration_holds(records: list[Record]) -> None:
    """``head`` meets every expectation, and says which fairness it assumes.

    The fairness assertion is not decoration. A liveness pass is only a claim
    about the paths fairness admits, so a report line that could not name the
    tags in force would not be evidence for the property it claimed.
    """
    head = _of(records, "head")
    assert set(head) == {*PROPERTIES, *PROBES}, (
        f"head reported {sorted(head)}, not the six properties and seven probes"
    )
    for subject in PROPERTIES:
        record = head[subject]
        assert record["met"] is True, record
        assert record["verdict"] == "holds", record
        assert record["fairness"], (
            f"{subject} passed without naming the fairness it assumes"
        )
    for subject in PROBES:
        record = head[subject]
        assert record["met"] is True, record
        assert record["verdict"] == "reached", record


def test_mutants_fail(records: list[Record]) -> None:
    """Each targeted mutant produces exactly the counterexample it predicts.

    The expected kind (a lasso, or a finite path for the seeded fault) is
    checked by ``modelcheck`` against the configuration's ``fails_as``, so
    asserting that the one failing subject is the predicted one also asserts
    the kind: a wrong kind is reported as ``wrong counterexample kind`` and
    would not be met.
    """
    for configuration, broken in LASSOS.items():
        mutant = _of(records, configuration)
        assert all(record["met"] is True for record in mutant.values()), mutant
        failing = [
            subject
            for subject, record in mutant.items()
            if record["verdict"] == "counterexample as expected"
        ]
        assert failing == [broken], (
            f"{configuration} should fail only {broken}, not {failing}"
        )


def test_seeded_fault_fails(records: list[Record]) -> None:
    """Deleting the ``cached_moving`` guard still yields a finite S1 failure.

    S1 is a safety property, so its counterexample is a reachable state rather
    than a loop. A lasso here would mean the seeded fault changed which
    property the run reports, which is a different model from the one the
    configuration claims to be.
    """
    seeded = _of(records, SEEDED)
    assert all(record["met"] is True for record in seeded.values()), seeded
    failing = [
        subject
        for subject, record in seeded.items()
        if record["verdict"] == "counterexample as expected"
    ]
    assert failing == ["S1"], f"{SEEDED} should fail only S1, not {failing}"
