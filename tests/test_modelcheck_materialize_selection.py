"""A run keeps exactly the assertions it is judged by, and no others.

Selection is what makes a pass mean something. FizzBee stops exploring when an
``exists`` probe is unreachable, prints no liveness result at all, and reports
the run as a success, so an unnamed probe left in the text would let a held
property be judged from a run that never checked it. The control that hid this
defect is pinned here on the real model, because the recorded run it replayed
had exactly that shape.

The identity and locality claims the rewrite also makes are in
:mod:`tests.test_modelcheck_materialize`; the properties that search a space
are in :mod:`tests.test_modelcheck_materialize_properties`.
"""

from pathlib import Path

import modelcheck_cases as cases
from modelcheck_materialize_support import kept, kept_by

from modelcheck import domain, materialize
from modelcheck.adapters.configuration import TomlConfigurations


def test_a_configuration_naming_no_assertion_strips_nothing() -> None:
    """A configuration that names no assertion asks for no selection.

    Assertion selection is driven by the names a configuration gives, so a
    configuration that gives none is the identity on the model's assertions.
    It cannot become a run that checks nothing: the run service refuses a
    configuration with no expectations before the checker is invoked.
    """
    assert kept_by((), name="head") == {"UnderTen", "ReachesTen", "ReachesOne"}, (
        "nothing is named, so nothing is stripped"
    )


def test_a_positive_run_keeps_only_the_assertions_it_names() -> None:
    """A configuration without an expected failure still selects its own names.

    A positive run is judged by the properties and probes its configuration
    claims, so every other assertion is stripped. Keeping them would widen the
    claim: the run would be about properties the report never mentions.
    """
    kept = kept_by((domain.Holds("UnderTen"), domain.Reachable("ReachesOne")))

    assert kept == {"UnderTen", "ReachesOne"}, "only the named assertions survive"


def test_a_positive_run_strips_an_unnamed_probe() -> None:
    """A probe the configuration does not name is removed, not left behind.

    This is the vacuity the L1 control hid: FizzBee stops exploring when an
    ``exists`` probe is unreachable, prints no liveness result, and the
    adapter reports a pass. With no unnamed probe left in the text, a pass can
    only mean the retained properties were actually checked.
    """
    assert kept_by((domain.Holds("UnderTen"),)) == {"UnderTen"}, (
        "the unnamed probe is stripped"
    )


def test_a_mixed_configuration_allows_its_positive_run() -> None:
    """A configuration expecting both a hold and a failure has a positive run.

    The run service asks for the run that should violate nothing before the
    targeted runs, so materializing it must not be refused for naming an
    expected failure. The positive run keeps the held assertion and strips the
    failure, which the targeted run exists to find.
    """
    mixed = (
        domain.Holds("UnderTen"),
        domain.FailsAs("ReachesTen", domain.CounterexampleKind.LASSO),
    )

    assert kept_by(mixed, name="mixed") == {"UnderTen"}, (
        "the expected failure belongs to its own run"
    )


def test_an_eventually_always_assertion_is_a_header_like_any_other() -> None:
    """The ``eventually always`` spelling is found, retained and stripped.

    The RFC documents it for liveness properties. A header the reader does not
    recognize is left in the text by the strip, so a property the configuration
    never names would still be checked by a run whose report does not mention
    it; and a configuration that does name it would be refused as expecting an
    assertion the model does not declare.
    """
    text = """\
action Init:
  n = 0

always assertion Held:
  return n < 10

eventually always assertion Unnamed:
  return n >= 10
"""
    naming_it = cases.configuration("named", expectations=(domain.Holds("Unnamed"),))
    naming_the_other = cases.configuration(
        "other", expectations=(domain.Holds("Held"),)
    )

    assert kept(
        materialize.materialize(text, naming_it, expected_failure=None).text
    ) == {"Unnamed"}, "a named liveness assertion is declared and retained"
    assert kept(
        materialize.materialize(text, naming_the_other, expected_failure=None).text
    ) == {"Held"}, "an unretained one is stripped like any other property"


def test_the_l1_control_retains_only_the_property_it_claims() -> None:
    """The real control's positive run keeps L1 and no unnamed probe.

    This reproduces the defect the control hid: with the model's other
    assertions in the text, FizzBee stopped at an unreachable probe the
    control never named, printed no liveness result, and the run was judged as
    L1 holding without L1 ever being checked.
    """
    source = TomlConfigurations(
        Path(__file__).resolve().parents[1] / "models" / "admission" / "transaction"
    )
    configuration = next(
        candidate
        for candidate in source.configurations()
        if candidate.name == "head-retention-idle-queue"
    )

    spec = materialize.materialize(
        source.source_text(), configuration, expected_failure=None
    )

    assert "assertion L1:" in spec.text, "the claimed property is kept"
    for name in (
        "S2",
        "L3",
        "L4b",
        "StopIssued",
        "TokenReleasedByAccounting",
        "ReachesRestarting",
        "GroupServerDrain",
    ):
        assert f"assertion {name}:" not in spec.text, f"{name} is stripped"
