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

import collections.abc as cabc
import re
from pathlib import Path

import pytest
from modelcheck_redaction_cases import (
    assert_detail_replaces,
    errored_run,
    lasso_run,
)

from modelcheck.adapters.fizzbee_replacements import WITHHELD

_STATE_BLOCK = re.compile(r'"state": \{[^}]*\}')
"""One flat state block in the recorded lasso, whatever values it holds."""


def _replace_each_state(text: str, states: cabc.Iterable[str]) -> str:
    """Rewrite the lasso's state blocks, one replacement per node in order.

    Parameters
    ----------
    text : str
        The recorded lasso's JSON text.
    states : collections.abc.Iterable[str]
        One JSON object, as text, for each state block, in node order.

    Returns
    -------
    str
        The text with as many state blocks replaced as ``states`` names.
    """
    pieces: list[str] = []
    position = 0
    for match, state in zip(_STATE_BLOCK.finditer(text), states, strict=False):
        pieces.extend((text[position : match.start()], f'"state": {state}'))
        position = match.end()
    pieces.append(text[position:])
    return "".join(pieces)


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
    nested = state[WITHHELD]
    assert isinstance(nested, dict), "the nested object is still an object"

    assert len(state) == 2, "both withheld top-level keys survive"
    assert all(key.startswith(WITHHELD) for key in state), "and both are withheld"
    assert len(nested) == 2, "so do the withheld keys inside it"


def test_a_withheld_key_keeps_one_name_across_states(tmp_path: Path) -> None:
    """A key keeps its numbered name when other keys come, go or reorder.

    Numbering from one state alone would rename ``b0`` as soon as ``a0`` left
    the state, and the saved trace would show a value moving between
    variables. The name is assigned on first sight and reused, so the trace
    names the same variable the same way throughout even as the state around
    it changes.
    """
    result = lasso_run(
        tmp_path,
        lambda text: _replace_each_state(
            text,
            ['{"a0": 1, "b0": 2}', '{"b0": 2}', '{"b0": 2, "a0": 1}'],
        ),
        {"MODELCHECK_E2E_SECRET": "0"},
    )

    second = f"{WITHHELD} #2"
    assert result.trace.states[0] == {WITHHELD: 1, second: 2}, "names are assigned"
    assert result.trace.states[1] == {second: 2}, "b0 keeps its name when alone"
    assert result.trace.states[2] == {second: 2, WITHHELD: 1}, "and when reordered"


def test_a_one_character_credential_before_a_full_stop_is_redacted(
    tmp_path: Path,
) -> None:
    """A sentence's full stop is punctuation after the value, not part of it.

    The rule refuses a character followed by a letter or a digit, because
    ``=0.5`` is one number the model computed and ``=01`` is two digits. A full
    stop, though, is how the line ends, so the character before it still stands
    where a value stands. Only a full stop beginning a fraction keeps the
    character, and the decimal is why the wider rule cannot simply be dropped:
    rewriting ``=0.5`` would put a placeholder inside a count.
    """
    result = errored_run(
        tmp_path,
        "#!/usr/bin/env bash\n"
        "printf 'token=%s.\\n' \"$MODELCHECK_E2E_SECRET\"\n"
        "printf 'token %s.\\n' \"$MODELCHECK_E2E_SECRET\"\n"
        'printf \'"%s".\\n\' "$MODELCHECK_E2E_SECRET"\n'
        "printf 'ratio=0.5\\n'\n",
        {"MODELCHECK_E2E_SECRET": "x"},
    )

    for kept, gone in (
        ("token=<redacted>.", "token=x."),
        ("token <redacted>.", "token x."),
        ('"<redacted>".', '"x".'),
        ("ratio=0.5", "ratio=<redacted>"),
    ):
        assert_detail_replaces(result.detail, kept=kept, gone=gone)


def test_a_key_that_already_reads_as_a_suffixed_name_is_kept(
    tmp_path: Path,
) -> None:
    """A genuine key equal to a generated name is not overwritten by one.

    A state can name a variable with the very text the numbering would
    produce, because that text holds no credential and so redacts to itself.
    Both mapping levels must then keep every entry: the withheld keys take the
    next free number rather than the one the genuine key already holds. The
    credential is ``0`` rather than the ``#`` the marker happens to contain, so
    the supplied marker really is an input key that survives and the collision
    is with an unchanged name.
    """
    marker = f'"{WITHHELD} #2"'
    state = (
        f'{{"box": {{{marker}: 1, "a0": 2, "b0": 3}}, {marker}: 4, "a0": 5, "b0": 6}}'
    )

    result = lasso_run(
        tmp_path,
        lambda text: _replace_each_state(text, [state, state, state]),
        {"MODELCHECK_E2E_SECRET": "0"},
    )

    second = f"{WITHHELD} #2"
    third = f"{WITHHELD} #3"
    recorded = result.trace.states[0]
    nested = recorded["box"]

    assert isinstance(nested, dict), "the nested object is still an object"
    assert len(recorded) == 4, "the top level keeps every entry"
    assert len(nested) == 3, "so does the nested object"
    assert recorded[second] == 4, "the genuine marker keeps its own entry"
    assert nested[second] == 1, "at both mapping levels"
    assert recorded[WITHHELD] == 5, "the first withheld key takes the marker"
    assert nested[WITHHELD] == 2, "at both mapping levels"
    assert recorded[third] == 6, "the second takes the next free number"
    assert nested[third] == 3, "at both mapping levels"
