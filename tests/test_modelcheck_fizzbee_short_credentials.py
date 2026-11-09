"""A one-character credential is protected by position, and by string.

A value of one character cannot be a needle of its own: it occurs in every
count, so replacing it wherever it appears would rewrite the evidence the
record exists for. The rule therefore follows the position — after an
assignment, between quotes, or after the label a checker prints beside the
value — and a decoded string, which is one value the checker produced, is
withheld when it still contains the character.

The runs come from :mod:`tests.modelcheck_redaction_cases`; the policy they
exercise is stated in :mod:`modelcheck.adapters.fizzbee_redaction`.
"""

from pathlib import Path

import pytest
from modelcheck_redaction_cases import (
    assert_detail_replaces,
    errored_run,
    lasso_run,
)

from modelcheck.adapters.fizzbee_replacements import WITHHELD


def test_a_one_character_credential_printed_beside_its_label_is_redacted(
    tmp_path: Path,
) -> None:
    """A value named by its label is a value position, not a word in the text.

    A checker need not print an assignment: it can name the variable it was
    handed beside the value, as ``token x``. Nothing but the label marks that
    character as a value, so the label is matched too, and the transcript keeps
    the part that says what was printed. The counts beside it are why the rule
    is positional: the same character must not rewrite them.
    """
    result = errored_run(
        tmp_path,
        "#!/usr/bin/env bash\n"
        "printf 'token %s\\nValid Nodes: 4\\n' \"$MODELCHECK_E2E_SECRET\"\n",
        {"MODELCHECK_E2E_SECRET": "x"},
    )

    assert_detail_replaces(result.detail, kept="token <redacted>", gone="token x")
    assert "Valid Nodes: 4" in result.detail, "a count is not a value"


@pytest.mark.parametrize("character", ["x", "#"])
def test_a_decoded_string_containing_a_one_character_credential_is_withheld(
    tmp_path: Path, character: str
) -> None:
    """A state built from a one-character value is withheld, not recorded.

    The text rule sees a value only where a label or a quote marks it, so a
    checker that built a state out of an inherited character leaves it inside a
    longer word. The string is one value the checker produced, so the whole
    string is withheld rather than kept with the character recoverable. A
    punctuation character is no different: it is as recoverable from the string
    as a letter is.
    """
    embedded = f"prefix{character}"

    result = lasso_run(
        tmp_path,
        lambda text: text.replace('"b": 0', f'"b": "{embedded}"'),
        {"MODELCHECK_E2E_SECRET": character},
    )

    recorded = [str(state) for state in result.trace.states]
    assert any(WITHHELD in state for state in recorded), "the string is withheld"
    assert not any(embedded in state for state in recorded), (
        "so the character is not recoverable from it"
    )


def test_state_keys_that_redact_alike_all_survive(tmp_path: Path) -> None:
    """Two keys withheld to one marker keep both entries under distinct names.

    :meth:`Redaction.retain_value` withholds every string that contains a
    one-character credential, so two variables whose names both hold that
    character become the same text. A dictionary built straight from those
    redactions keeps only the last entry, and the saved trace silently loses a
    variable. The repeats are therefore numbered, at the top level and inside
    a nested object alike.
    """
    result = lasso_run(
        tmp_path,
        lambda text: text.replace(
            '"b": 0,\n        "g": 0',
            '"b0": {"x0": 1, "y0": 2},\n        "g0": 3',
        ),
        {"MODELCHECK_E2E_SECRET": "0"},
    )

    state = result.trace.initial
    expected = {WITHHELD, f"{WITHHELD} #2"}

    assert set(state) == expected, "both withheld top-level keys survive"
    nested = state[WITHHELD]
    assert isinstance(nested, dict), "the nested object is still an object"
    assert set(nested) == expected, "so do the withheld keys inside it"
