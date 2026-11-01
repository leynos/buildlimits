"""The materializer changes what its configuration names, and nothing else.

A variant that quietly rewrote more than it was asked to would invalidate every
run of that configuration, and nothing downstream would notice: the checker
would still answer, and the answer would be about a different model. The
contract is therefore stated as properties rather than examples — the identity
on a configuration that changes nothing, idempotence, and locality — and the
examples that would be easiest to get wrong are pinned explicitly here.

The refusals live in :mod:`tests.test_modelcheck_materialize_errors`: a
configuration that names something the model does not contain must fail loudly,
which is a different claim from "a rewrite touches only what it names" and
would obscure it. The generated-text properties and the mutant control live in
:mod:`tests.test_modelcheck_materialize_properties`, because they search a
space rather than pin an example.

Every test here was written before its module was implemented (EP-M2), so it
pins a contract the stub could not satisfy.
"""

import typing as typ

import modelcheck_cases as cases

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


def test_an_empty_configuration_is_the_identity_on_the_body() -> None:
    """A configuration that changes and drops nothing returns the body.

    The byte-for-byte comparison is the strongest statement of locality there
    is: comments, blank lines, trailing spaces and the absence of a final
    newline all survive, which a line-by-line rebuild would silently lose.
    Frontmatter is the exception — a configuration with no options replaces it
    with nothing rather than inheriting the fixed bounds — so the sources here
    carry none.

    Fairness is not part of the identity claim. A configuration that names no
    table still leaves the model's own annotations in force, so the tag the
    first source carries is reported even though the configuration mentioned
    none: the report quotes the run rather than the configuration.
    """
    from modelcheck import materialize

    for source, carried in (
        (cases.FIXED, ("F1",)),
        ("", ()),
        ("action Init:\n  n = 0", ()),
    ):
        spec = materialize.materialize(
            source, cases.configuration("head"), expected_failure=None
        )
        assert spec.text == source, f"{source!r} is returned unchanged"
        assert spec.fairness_in_force == carried, f"{source!r} carries {carried}"


def test_an_empty_configuration_strips_no_assertion() -> None:
    """With no expected failure, every assertion the model declares survives.

    A run with no expected failure is how a fixed configuration checks all of
    its properties at once, so dropping one would silently reduce what the
    configuration claims.
    """
    from modelcheck import materialize

    spec = materialize.materialize(
        cases.TAGGED, cases.configuration("head"), expected_failure=None
    )

    for name in ("UnderTen", "ReachesTen", "ReachesOne"):
        assert f"assertion {name}:" in spec.text, f"{name} survives"


def test_a_named_constant_is_replaced_and_the_rest_is_untouched() -> None:
    """Only the named constant line changes, including its inline comment.

    The comment is kept because the model's comments are its documentation;
    a rewrite that dropped them would make a variant unreadable and would hide
    the ``# transcribes:`` citation a known-bad switch needs.
    """
    from modelcheck import materialize

    source = "CAPACITY = 3  # the W1 bound\nOTHER = 1\n\naction Init:\n  n = 0\n"
    spec = materialize.materialize(
        source,
        cases.configuration("small", constants={"CAPACITY": "2"}),
        expected_failure=None,
    )

    assert "CAPACITY = 2  # the W1 bound\n" in spec.text, "the comment survives"
    assert "OTHER = 1\n" in spec.text, "another constant is untouched"
    assert "CAPACITY = 3" not in spec.text, "the old value is gone"


def test_a_tagged_action_takes_its_level_and_keeps_its_tag() -> None:
    """Each of the two fairness levels spells its keyword, and the tag stays.

    ``weak`` is the bare ``fair`` keyword and ``strong`` is ``fair<strong>``;
    the tag annotation survives either way, because it is the model's own
    record of which assumption the action carries and a report quotes it back.
    """
    from modelcheck import materialize

    weak = materialize.materialize(
        cases.FIXED,
        cases.configuration("weak", fairness={"F1": "weak"}),
        expected_failure=None,
    )
    strong = materialize.materialize(
        cases.FIXED,
        cases.configuration("strong", fairness={"F1": "strong"}),
        expected_failure=None,
    )

    assert "atomic fair action Bump:      # fairness: F1" in weak.text, "weak spelling"
    assert "atomic fair<strong> action Bump:      # fairness: F1" in strong.text, (
        "strong spelling"
    )
    assert weak.fairness_in_force == ("F1",), "the tag stays in force"
    assert strong.fairness_in_force == ("F1",), "and so does the strong one"


def test_an_unfair_level_removes_the_keyword() -> None:
    """``unfair`` leaves no fairness keyword at all, which FizzBee accepts.

    The three levels the configuration may name are ``weak``, ``strong`` and
    ``unfair``; the last is expressed by the absence of the keyword rather than
    by a fourth spelling. An ``unfair`` tag is therefore not fairness in force,
    which is what the report must print.
    """
    from modelcheck import materialize

    spec = materialize.materialize(
        cases.FIXED,
        cases.configuration("unfair", fairness={"F1": "unfair"}),
        expected_failure=None,
    )

    body, _, _ = spec.text.partition("assertion")
    assert "atomic action Bump:      # fairness: F1" in body, "the keyword is gone"
    assert "fair action" not in body, "no fairness keyword remains"
    assert not spec.fairness_in_force, "an unfair tag is not fairness in force"


def test_a_configuration_naming_no_tag_reports_the_models_own() -> None:
    """The fairness a run carries is read from its text, not its configuration.

    A mutant the configuration table does not name inherits the model's own
    annotations, so its actions are still fair. Reporting the configuration's
    (empty) table would say the run assumed nothing and quietly turn every
    liveness verdict it produced into an unexplained one. The tagged source has
    one action carrying ``F1`` and one annotated ``F2`` action with no fairness
    keyword, so the expected answer is ``F1`` alone: an annotation is the
    model's record of an assumption, not proof the action is fair.
    """
    from modelcheck import materialize

    spec = materialize.materialize(
        cases.TAGGED, cases.configuration("head"), expected_failure=None
    )

    assert spec.fairness_in_force == ("F1",), "the model's carried tag is reported"


def test_disabling_one_tag_leaves_the_other_in_force() -> None:
    """An ablation reports the tags still carried, not the table it was given.

    The ``unfair`` level is expressed by removing the keyword, so the action
    keeps its annotation while ceasing to be fair. A report built from the
    configuration would still name both tags, which is what a mutant run must
    not claim: the point of the ablation is that one assumption is gone.
    """
    from modelcheck import materialize

    source = (
        "action Init:\n  n = 0\n\n"
        "atomic fair action StepOne:  # fairness: F1\n  n = n + 1\n\n"
        "atomic fair action StepTwo:  # fairness: F2\n  n = n + 1\n"
    )
    spec = materialize.materialize(
        source,
        cases.configuration("ablate-f1", fairness={"F1": "unfair"}),
        expected_failure=None,
    )

    assert spec.fairness_in_force == ("F2",), "only the disabled tag is gone"


def test_the_frontmatter_is_replaced_whole(snapshot: SnapshotAssertion) -> None:
    """Every option comes from the configuration, with nothing inherited.

    A variant that inherited ``max_actions`` from the fixed model would be
    exploring a different bound than its configuration states, which is exactly
    the truncation artefact the spike recorded. Strings are quoted so a value
    such as ``strict`` cannot be read as a bare word, and booleans are spelled
    the YAML way.

    The block is snapshot rather than probed for three substrings: the order,
    the indentation and the quoting are all part of what the checker reads, and
    a snapshot fails on any of them changing. A checker given a block it cannot
    parse reports a syntax error in a file this tool wrote, which names nothing
    useful.
    """
    from modelcheck import materialize

    spec = materialize.materialize(
        cases.TAGGED,
        cases.configuration(
            "bounded",
            options={
                "options": {"max_actions": 9, "max_concurrent_actions": 2},
                "liveness": "strict",
                "deadlock_detection": False,
            },
        ),
        expected_failure=None,
    )

    _, _, rest = spec.text.partition("---\n")
    block, _, body = rest.partition("---\n")

    assert block.splitlines() == snapshot, "the whole block is the configuration's"
    assert "max_actions: 4" not in spec.text, "nothing is inherited"
    assert "action Init:" in body, "the body follows the block"


def test_the_expected_failure_keeps_every_other_assertion() -> None:
    """Only the assertions that are neither retained nor expected survive (D3).

    The configuration keeps every assertion expected to hold plus exactly one
    expected failure. A run of one known-bad variant must therefore compile
    with its safety assertion still in place; otherwise the run could report
    the safety violation the model also contains and prove nothing about the
    liveness defect the configuration exists to find.
    """
    from modelcheck import domain, materialize

    configuration = cases.configuration(
        "incident-3",
        expectations=(
            domain.Holds("UnderTen"),
            domain.FailsAs("ReachesTen", domain.CounterexampleKind.LASSO),
        ),
    )

    spec = materialize.materialize(
        cases.TAGGED, configuration, expected_failure="ReachesTen"
    )

    assert "assertion UnderTen:" in spec.text, "an assertion expected to hold is kept"
    assert "assertion ReachesTen:" in spec.text, "the expected failure is kept"
    assert "assertion ReachesOne:" not in spec.text, (
        "an assertion neither expected nor failing is stripped"
    )


def test_materialize_is_idempotent() -> None:
    """Materializing the materialized text changes nothing further.

    Idempotence is what lets the adapter copy and re-run a spec — for example
    to replay a stored trace — without the second pass differing from the
    first. The source has no frontmatter and the configuration has options, so
    the first pass adds a block and the second must leave it alone.
    """
    from modelcheck import materialize

    configuration = cases.configuration(
        "both",
        constants={"CAPACITY": "2"},
        fairness={"F1": "strong"},
        options={"options": {"max_actions": 5}},
    )
    source = (
        "CAPACITY = 3\n"
        "action Init:\n  n = 0\n"
        "atomic fair action Bump:  # fairness: F1\n  n = n + 1\n"
    )

    once = materialize.materialize(source, configuration, expected_failure=None)
    twice = materialize.materialize(once.text, configuration, expected_failure=None)

    assert twice.text == once.text, "the second pass changes nothing"
    assert twice.fairness_in_force == once.fairness_in_force == ("F1",), (
        "and reports the same fairness"
    )
