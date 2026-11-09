"""A run's evidence carries no fact about the runner that produced it.

The record a failed run keeps is committed, so it must describe the model and
not the machine that checked it. Two things can put a runner fact into it: the
checker can print one, and the checker can write one into the error graph it is
asked for. Both are redacted here, in every spelling a real encoder produces,
and the redaction happens *after* the run has been classified — a replacement
is not injective, so sanitizing the graph first could change its validity, its
ending, or the equality that makes a lasso a lasso.

A text a credential of two or more characters survived in is withheld rather
than recorded. That rule is asserted here too, because the alternative —
committing the text and hoping the needles were enough — is the failure this
module exists to catch. A one-character credential embedded in free diagnostic
text is the accepted residual, pinned by its own test elsewhere.

The stub launcher and the specification it runs come from
:mod:`tests.fizzbee_process_stubs`; the runs around each test's own graph or
transcript come from :mod:`tests.modelcheck_evidence_cases`, and the typed
preconditions and the trace check from :mod:`tests.modelcheck_redaction_cases`.
"""

import typing as typ
from pathlib import Path

import pytest
from modelcheck_evidence_cases import graph_run, run_stub
from modelcheck_redaction_cases import (
    assert_detail_replaces,
    assert_trace_hides,
    errored_graph,
    errored_run,
    lasso_run,
)

from modelcheck import domain
from modelcheck.adapters.fizzbee_redaction import redaction_for
from modelcheck.adapters.fizzbee_replacements import (
    HIDDEN_VALUE,
    WITHHELD,
    Redaction,
    Replacement,
)

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


def test_a_scratch_path_in_the_error_graph_is_redacted(tmp_path: Path) -> None:
    """A graph kept as evidence is redacted like the transcript beside it.

    The graph is checker-written text about the model, but it is written into
    the run's scratch directory and can name that directory. Discarded as
    evidence it would put a runner path into the committed record exactly as a
    printed one would, so the adapter replaces it before the text leaves.
    """
    result = graph_run(
        tmp_path,
        'mkdir -p out\nprintf \'{"state": "%s"}\' "$PWD" > out/error-graph.json\n',
    )

    assert isinstance(result, domain.Errored), "a mapping is not an error graph"
    assert "the error graph could not be parsed" in result.detail, (
        "the refusal names the parse"
    )
    assert '{"state": "<scratch>"}' in result.detail, "and the runner path is replaced"
    assert ".model-check" not in result.detail, "with no part of it left behind"


def test_a_credential_in_a_malformed_graph_is_redacted(tmp_path: Path) -> None:
    """A graph refused before it can be a trace still loses its credential.

    The refusal quotes the document it refused, and it quotes the *decoded*
    value, so a credential the checker escaped in a spelling the text needles
    do not know — Go writes a less-than sign as a six-character escape — reaches
    the reason as the value itself. The reason is evidence, so it is redacted
    like the graph beside it, and the graph's own spelling is replaced too.
    """
    result = errored_graph(
        tmp_path, '{"state": "a\\u003cb"}', {"MODELCHECK_E2E_SECRET": "a<b"}
    )

    assert "<redacted>" in result.detail, "the decoded value is replaced"
    assert "a<b" not in result.detail, "and the credential is not recorded"
    assert "\\u003c" not in result.detail, "nor any spelling of it in the capture"


def test_a_credential_a_stopped_run_printed_is_redacted(tmp_path: Path) -> None:
    """A stopped run's marker line is evidence, so it is redacted too.

    The checker prints the marker when a signal cuts the run short, and the
    line can carry whatever it echoed around the interruption. The record
    persists the detail, so a credential in it would be committed exactly as
    one in an error's detail would.
    """
    result = run_stub(
        tmp_path,
        "#!/usr/bin/env bash\n"
        "printf 'Model checker stopped: token=%s\\n' \"$MODELCHECK_E2E_SECRET\"\n",
        {"MODELCHECK_E2E_SECRET": "s3cr3t-do-not-record"},
    )

    assert isinstance(result, domain.Stopped), "the marker is a stopped run"
    assert "token=<redacted>" in result.detail, "the value is replaced"
    assert "s3cr3t-do-not-record" not in result.detail, "and not recorded"


@pytest.mark.parametrize("echoed_value", ["s3cr3t-do-not-record", "a1b2c"])
def test_a_credential_the_checker_printed_is_redacted(
    tmp_path: Path, echoed_value: str
) -> None:
    """A credential the checker echoes does not reach the record.

    The checker inherits the caller's environment, so anything in it is a value
    the checker can print. The adapter cannot tell a secret from any other value
    except by the variable's name, so it replaces the value of every
    credential-named variable before the transcript becomes evidence. The stub
    echoes the value it was given, which is what makes this a test of the
    redaction rather than of a value that never arrived.

    The length of a value says nothing about whether it is secret, so a short
    one is replaced too. It is matched only where it stands alone, which is
    where a checker prints a value it was given, so the replacement cannot
    rewrite the middle of an ordinary word.
    """
    result = errored_run(
        tmp_path,
        "#!/usr/bin/env bash\nprintf 'token=%s\\n' \"$MODELCHECK_E2E_SECRET\"\n",
        {"MODELCHECK_E2E_SECRET": echoed_value},
    )

    assert "token=<redacted>" in result.detail, "the value is replaced whole"
    assert echoed_value not in result.detail, "and no part of it is left behind"


def test_both_json_spellings_of_a_credential_are_redacted(tmp_path: Path) -> None:
    r"""A graph holds a spelling of a credential, and there is more than one.

    Two JSON writers both produce valid documents and disagree about the
    characters they escape: a non-ASCII character may be written literally or
    as a Unicode escape, and a quote is escaped by both. A credential mixing the
    two — ``a"é`` — therefore appears in the graph in a shape that is neither
    the value itself nor a single "escaped form" of it. Only replacing every
    spelling keeps the record clean, so the graph written here carries each one.
    """
    result = errored_graph(
        tmp_path,
        '{"escaped": "a\\"\\u00e9", "literal": "a\\"é"}',
        {"MODELCHECK_E2E_SECRET": 'a"é'},
    )

    assert result.detail.count("<redacted>") >= 2, "each spelling is replaced"
    assert "\\u00e9" not in result.detail, "and neither is left behind"
    assert "é" not in result.detail, "in its literal spelling either"
    assert 'a\\"' not in result.detail, "nor the escaped quote around them"


def test_a_credential_only_the_decoder_sees_is_redacted(tmp_path: Path) -> None:
    """A spelling the needles do not anticipate is still caught in the trace.

    The text pass knows the spellings :mod:`json` produces, but a checker
    written in another language escapes more: Go's encoder writes a less-than
    sign as a Unicode escape of six characters, which no Python writer produces
    for that character. Replacing the graph's text cannot find a credential
    spelled that way, so the decoded path is redacted too — and that is what
    this test observes, because the trace is the value the record and the saved
    trace file carry.
    """
    credential = "a<b"
    result = lasso_run(
        tmp_path,
        lambda text: text.replace('"b": 0', '"b": "a\\u003cb"'),
        {"MODELCHECK_E2E_SECRET": credential},
    )

    assert_trace_hides(result, credential)


def test_a_one_character_credential_named_flag_leaves_the_counts_intact(
    tmp_path: Path,
) -> None:
    """A credential-named flag is not a secret, and a count is not a value.

    The name heuristic matches ``*_SESSION_*``, which shells use for booleans
    such as ``CLAUDE_CODE_SESSION_ATTENDED=0``. A bare one-character needle
    would make ``0`` and ``1`` needles: every state count in the transcript and
    every number in the error graph would become a placeholder, and the graph
    would no longer be JSON. The value is therefore replaced only in a value
    position, so the counts beside it survive.
    """
    result = errored_run(
        tmp_path,
        '#!/usr/bin/env bash\nprintf \'{"current": 0, "counts": {"Gate": 1}}\\n\'\n',
        {"MODELCHECK_E2E_SESSION_ATTENDED": "0"},
    )

    assert '{"current": 0, "counts": {"Gate": 1}}' in result.detail, (
        "the counts survive, because a count is not a value the checker was given"
    )


def test_a_one_character_credential_in_a_value_position_is_redacted(
    tmp_path: Path, snapshot: SnapshotAssertion
) -> None:
    """A one-character credential is replaced where a checker echoes it.

    The same value that must not rewrite a count is still a credential when it
    is the value of an assignment or a JSON string. Both positions are where a
    checker prints a variable it inherited, so both are replaced while the
    number beside them is left alone. The whole diagnostic is pinned, because
    which positions it replaces and which it leaves is the contract.
    """
    result = errored_run(
        tmp_path,
        '#!/usr/bin/env bash\nprintf \'token=0\\n{"current": 0, "flag": "0"}\\n\'\n',
        {"MODELCHECK_E2E_SESSION_ATTENDED": "0"},
    )

    assert result.detail == snapshot, "the value positions are replaced, the count kept"


def test_a_one_character_credential_in_a_decoded_state_is_redacted(
    tmp_path: Path,
) -> None:
    """A decoded state value is a value position, even without quotes.

    The text rule replaces a one-character credential only after ``=`` or
    between quotes, because a bare character needle would rewrite every count.
    A decoded state value has neither, yet the whole string is the value the
    checker was given, so it is replaced rather than left in the saved trace.
    """
    result = lasso_run(
        tmp_path,
        lambda text: text.replace('"b": 0', '"b": "x"'),
        {"MODELCHECK_E2E_SECRET": "x"},
    )

    assert_trace_hides(result, "x")


def test_an_equivalent_escape_spelling_withholds_the_text() -> None:
    r"""A spelling no needle enumerates is still the credential.

    The value ``a<b`` can be written with every character escaped, which is
    valid JSON and matches neither the literal value nor the Go spelling the
    needles know. The survival check decodes the text before it looks, so the
    text is withheld rather than recorded with the value recoverable from it.
    """
    redaction = Redaction(
        (Replacement("a<b", HIDDEN_VALUE, whole_word=True),), ("a<b",)
    )

    assert redaction.retain('{"token": "\\u0061\\u003c\\u0062"}') == WITHHELD, (
        "the spelling decodes to the credential, so the text is withheld"
    )


def test_evidence_a_credential_survives_in_is_withheld(tmp_path: Path) -> None:
    """A text a replacement could not clean is not recorded at all.

    A short credential is replaced only where it stands alone, so a checker
    that printed it inside a longer token would leave it in the transcript. No
    rule can tell that from a coincidence without rewriting text that is not a
    secret, so the adapter withholds the transcript and says why rather than
    committing the value.
    """
    result = errored_run(
        tmp_path,
        "#!/usr/bin/env bash\nprintf 'token=xabcy\\n'\n",
        {"MODELCHECK_E2E_SECRET": "abc"},
    )

    assert WITHHELD in result.detail, "the transcript is withheld, and says so"
    assert "abc" not in result.detail, "so the credential is not recorded"


def test_a_numeric_credential_does_not_invalidate_the_graph(tmp_path: Path) -> None:
    """A credential equal to a JSON number is replaced after classification.

    Replacing the graph's text before parsing would turn ``"b": 12`` into
    ``"b": <redacted>``, which is not JSON, and a valid lasso would be reported
    as a run that proved nothing. The graph is classified as the checker wrote
    it, so the verdict survives; the number is a value the model computed and
    is left as it is.
    """
    result = lasso_run(
        tmp_path,
        lambda text: text.replace('"b": 0', '"b": 12'),
        {"MODELCHECK_E2E_SECRET": "12"},
    )

    assert result.kind is domain.CounterexampleKind.LASSO, (
        "the ending is read from the graph the checker wrote"
    )


def test_two_credentials_that_redact_alike_keep_the_lasso(tmp_path: Path) -> None:
    """Two distinct state values may become one placeholder without a new verdict.

    A lasso is classified by a state repeating. Replacing the graph's text
    first could make two different states equal and change the ending, so the
    graph is classified as the checker wrote it and the values are replaced
    only in the evidence the record carries.
    """

    def edit(text: str) -> str:
        """Write the path's three state values, the first one repeated."""
        for value in ("a1b2c", "d4e5f", "a1b2c"):
            text = text.replace('"b": 0', f'"b": "{value}"', 1)
        return text

    result = lasso_run(
        tmp_path,
        edit,
        {"MODELCHECK_E2E_SECRET_A": "a1b2c", "MODELCHECK_E2E_SECRET_B": "d4e5f"},
    )

    assert result.kind is domain.CounterexampleKind.LASSO, (
        "the ending is read before either value is replaced"
    )
    assert_trace_hides(result, "a1b2c", "d4e5f")


def test_a_runner_path_beside_the_scratch_directory_is_redacted(
    tmp_path: Path,
) -> None:
    """A home path in the transcript is a runner path, so it is replaced too.

    The scratch directory is not the only path the checker can print. A cache
    or module path under the caller's home directory identifies the runner just
    as plainly, and the adapter is handed the home directory because it is in
    the environment the checker ran with.
    """
    result = errored_run(
        tmp_path,
        "#!/usr/bin/env bash\nprintf 'cache %s/.cache/go-build\\n' \"$HOME\"\n",
        {"HOME": "/home/runner"},
    )

    assert_detail_replaces(
        result.detail, kept="cache <home>/.cache/go-build", gone="/home/runner"
    )


@pytest.mark.parametrize(
    ("environment", "text"),
    [
        ({"HOME": "/"}, "reading /opt/x"),
        ({"TMPDIR": "tmp"}, "tmp/x"),
    ],
    ids=["root-home", "single-component-tmpdir"],
)
def test_a_path_that_names_no_directory_is_not_a_needle(
    environment: dict[str, str], text: str
) -> None:
    """A root or single-component path is not replaced.

    ``HOME=/`` and a one-component ``TMPDIR`` would each make a needle that
    rewrites ordinary text — a ``/`` needle replaces every slash — without
    naming a directory the run used. The guard keeps them out of the needles,
    so text that merely contains the character is left alone.
    """
    redaction = redaction_for(Path(".model-check/run"), Path("/opt/fizz"), environment)

    assert redaction.apply(text) == text, "the value names no directory to replace"


def test_a_multi_component_home_is_still_replaced() -> None:
    """The guard narrows the rule rather than removing it.

    A home that names a directory is still replaced, which is what makes the
    guard a condition on the value's shape rather than a way to skip every
    environment path.
    """
    redaction = redaction_for(
        Path(".model-check/run"), Path("/opt/fizz"), {"HOME": "/home/runner"}
    )

    assert redaction.apply("/home/runner/x") == "<home>/x", "the home is replaced"


def test_a_scratch_path_the_checker_printed_is_redacted(tmp_path: Path) -> None:
    """A runner path is not evidence about the model, so it is replaced.

    The scratch directory's absolute path identifies this machine and this run
    and nothing about the model. A record that carried it would put a runner
    path into an artefact a maintainer commits, so the adapter replaces it
    before the text becomes a diagnostic.
    """
    result = errored_run(
        tmp_path,
        "#!/usr/bin/env bash\nprintf 'reading %s/model.fizz\\n' \"$PWD\"\n",
    )

    assert "<scratch>/model.fizz" in result.detail, "the path is replaced"
    assert ".model-check" not in result.detail, "and the runner path is gone"
