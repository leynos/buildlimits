"""The checker's run-record format stops at the adapter.

An error graph is the checker's document, and its keys name nothing in the
domain. A pure module that mentions one depends on a format the checker is
free to change, and no runtime test would notice: the dependency is a string
in a decoder or a docstring. The rule reads each pure module's text for exactly
that reason.

The model's own source language — frontmatter, assertions, fairness tags — is
deliberately *not* covered. That syntax is the domain's input rather than an
external tool's output, so `materialize`, `frontmatter` and `assertions` may
read and write it.

This lives beside the layering rules rather than inside them so both modules
stay under the file-length limit. `PURE` and `PACKAGE` are imported from
`test_modelcheck_layering` so there is one definition of which modules are
pure.
"""

import pytest
from test_modelcheck_layering import PACKAGE, PURE

CHECKER_OUTPUT_VOCABULARY = (
    "error-graph",
    "Node.state",
    '"Type"',
    '"Name"',
)
"""The names of the checker's output format, which only an adapter may know.

An error graph is the checker's document, not the model's: its keys name
nothing in the domain, so a pure module that mentions one is depending on a
format the checker is free to change. The model's own source language —
frontmatter, assertions, fairness tags — is deliberately *not* in this list.
That syntax is the domain's input rather than an external tool's output, so
``materialize``, ``frontmatter`` and ``assertions`` may read and write it; only
the record of a finished run has to be translated first.
"""


def _checker_words(text: str) -> list[str]:
    """Return the checker output-format names a source text mentions.

    Parameters
    ----------
    text : str
        A module's source text.

    Returns
    -------
    list[str]
        Every vocabulary word the text contains, in the tuple's order.
    """
    return [word for word in CHECKER_OUTPUT_VOCABULARY if word in text]


@pytest.mark.parametrize("module", PURE)
def test_a_pure_module_names_no_part_of_the_checkers_output_format(module: str) -> None:
    """No pure module mentions a key of the checker's run record.

    The domain may know the model's source language — that is what it is
    written in — but a finished run's record is the checker's format. Decoding
    it is the adapter's job, and this rule is what keeps the decoding there:
    without it, a later convenience function could put ``json.loads`` and the
    checker's keys back into ``trace`` and every other test here would still
    pass.

    The rule reads the module's text rather than its tree because the leak it
    guards against can arrive as a string literal or as a docstring, and both
    are equally a dependency on the format. The cost is that a docstring
    explaining the boundary cannot quote a key, which is a fair price: the
    explanation belongs in the adapter, where the keys are.
    """
    words = _checker_words((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))

    assert not words, f"{module} names {words}"


def test_the_vocabulary_rule_rejects_a_planted_key() -> None:
    """The rule rejects a module that decodes the checker's record itself.

    The non-vacuity clause: a rule whose vocabulary had been emptied would pass
    every module, and one that named a key no module could contain would do the
    same. This fixture is the shape the rule exists to catch — a pure module
    indexing the record's own keys.
    """
    planted = 'def link(fields):\n    return fields["Type"], fields["Name"]\n'

    assert _checker_words(planted), "the vocabulary names what the leak looks like"
