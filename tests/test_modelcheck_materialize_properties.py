"""The materializer's rewrites, searched rather than pinned.

An example test says a rewrite did the right thing to the text somebody chose.
These two say it does the right thing to text nobody chose: a generated
constant, value, tag, level and noise comment, and then a second pass over the
result. They live apart from :mod:`tests.test_modelcheck_materialize` because
the property and its handwritten mutant control are one claim — the property
suite is only worth its run time if it rejects a materializer that over-rewrites
— and because a five-argument Hypothesis test would otherwise push that module
past the repository's 400-line limit.

Every test here was written before its module was implemented (EP-M2), so it
pins a contract the stub could not satisfy.
"""

import modelcheck_cases as cases
from hypothesis import example, given
from hypothesis import strategies as st

# Noise that cannot form an annotation: no colon, so a generated comment can
# never contain a second ``# maps:`` or ``# fairness:`` the materializer would
# be entitled to rewrite. Newlines are excluded too, so the noise stays one
# line.
_NOISE = st.characters(min_codepoint=32, max_codepoint=126).filter(
    lambda character: character != ":"
)
_NOISE_LINE = st.text(alphabet=_NOISE, max_size=40).map(lambda text: f"# {text}\n")
"""A comment line that carries no annotation."""


@given(
    name=cases.IDENTIFIER,
    value=st.integers(min_value=-1000, max_value=1000),
    tag=cases.IDENTIFIER,
    level=cases.LEVEL,
    noise=_NOISE_LINE,
)
@example(name="CAPACITY", value=2, tag="F1", level="strong", noise="# a comment\n")
@example(name="LIMIT", value=0, tag="F9", level="unfair", noise="#\n")
def test_rewrites_are_local_and_idempotent(  # pylint: disable=too-many-arguments,too-many-positional-arguments  # @given dictates one parameter per generated value.
    name: str,
    value: int,
    tag: str,
    level: str,
    noise: str,
) -> None:
    """A rewrite touches the named lines; a second rewrite changes nothing.

    Locality is checked by asserting the untouched lines back verbatim rather
    than by forbidding substrings: the generated noise may legitimately contain
    almost any text, so a forbidden-substring test would fail for a correct
    materializer. The two ``@example`` cases pin a comment after a rewritten
    constant, and an ``unfair`` rewrite, which are the shapes most likely to be
    lost.

    ``@given`` dictates one parameter per generated value, so the arity is the
    property's interface rather than an accident, and the pylint waiver is
    permanent.
    """
    from modelcheck import materialize

    source = (
        f"{name} = 3  # keep me\n"
        f"{noise}"
        "action Init:\n"
        "  n = 0\n"
        f"atomic fair action Bump:  # fairness: {tag}\n"
        "  n = n + 1\n"
        "always assertion Under:\n"
        "  return n < 10\n"
    )
    configuration = cases.configuration(
        "variant",
        constants={name: str(value)},
        fairness={tag: level},
        options={"options": {"max_actions": value}},
    )

    spec = materialize.materialize(source, configuration, expected_failure=None)

    assert f"{name} = {value}  # keep me\n" in spec.text, "the constant is rewritten"
    assert "action Init:\n  n = 0\n" in spec.text, "the body is untouched"
    assert "always assertion Under:\n  return n < 10\n" in spec.text, (
        "the assertion survives"
    )
    assert f"# fairness: {tag}" in spec.text, "the tag annotation stays"
    expected = () if level == "unfair" else (tag,)
    assert spec.fairness_in_force == expected, "only a fair tag is in force"
    again = materialize.materialize(spec.text, configuration, expected_failure=None)
    assert again.text == spec.text, "a second rewrite changes nothing"


def test_the_locality_property_rejects_a_mutant_materializer() -> None:
    """The locality predicate holds for the real output and not for a mutant.

    The mutant rewrites every ``fair`` keyword, so it turns an untagged action
    unfair. The predicate is applied to the real materializer's output first —
    so a predicate that rejects everything cannot pass this test — and then to
    the mutant, which it must reject. A property suite that passed for both
    would be proving nothing about either.
    """
    from modelcheck import materialize

    source = (
        "atomic fair action Tagged:  # fairness: F1\n"
        "  n = 1\n"
        "atomic fair action Untagged:\n"
        "  n = 2\n"
    )
    configuration = cases.configuration("mutant", fairness={"F1": "unfair"})

    actual = materialize.materialize(source, configuration, expected_failure=None).text

    assert _only_the_tagged_was_rewritten(actual), (
        "the predicate describes the materializer it is checking"
    )
    assert not _only_the_tagged_was_rewritten(_rewrite_every_fair(source)), (
        "the mutant over-rewrote and must be rejected"
    )


def _only_the_tagged_was_rewritten(text: str) -> bool:
    """Report whether the tagged action is the only one that lost its keyword.

    Parameters
    ----------
    text : str
        The materialized specification.

    Returns
    -------
    bool
        Whether ``Tagged`` no longer carries a keyword while ``Untagged``
        still does.
    """
    return "atomic action Tagged:" in text and "atomic fair action Untagged:" in text


def _rewrite_every_fair(source: str) -> str:
    """Return ``source`` with every ``fair`` keyword removed.

    This is the mutant the property is checked against: it does what a careless
    implementation would do — replace every occurrence of the keyword — and
    must be rejected.

    Parameters
    ----------
    source : str
        The model text.

    Returns
    -------
    str
        The text with each ``fair`` keyword deleted.
    """
    return source.replace("fair ", "")
