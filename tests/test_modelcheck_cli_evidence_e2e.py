"""The evidence a failed run persists is read back because it is committed.

These tests run the real command line into a written record and then read that
record. The adapter deletes the scratch directory as the run returns, so the
persisted detail is the only place the checker's transcript and its error graph
survive; what it carries is what a maintainer will have. That makes the record
the right place to assert on bounding and redaction: a test against an
in-process value could pass while the recorded text still carried a runner path
or a credential. A counterexample's trace is the exception: a matrix judgement
row records the verdict and its measurements, and the trace is written to the
saved-trace file, so the tests that read a trace assert on that file and guard
that the record still carries none.

The stub's modes are described in :mod:`tests.fixtures.modelcheck`; the helpers
that run it are shared with :mod:`tests.test_modelcheck_cli_e2e`.
"""

import json
import typing as typ
from pathlib import Path

import pytest
from modelcheck_e2e_cases import TracedRun, assert_holds, persisted, traced_run

from modelcheck.adapters.fizzbee_replacements import WITHHELD


def _persisted_detail(tmp_path: Path, answer: str, credential: str) -> str:
    """Run one stub answer with a credential and return the detail it persisted.

    Every test that reads a run's evidence needs the same three things before it
    can assert anything: a run whose credential is the one under test, the status
    that says the run proved nothing, and the detail the record kept. Sharing
    them keeps each test's assertions the only thing that differs.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The scratch directory the run owns.
    answer : str
        The stub's ``MODELCHECK_E2E_ANSWER``, which selects the checker output
        the run replays.
    credential : str
        The value the stub is given as ``MODELCHECK_E2E_SECRET``.

    Returns
    -------
    str
        The persisted detail of the first record.
    """
    status, written = persisted(tmp_path, answer, MODELCHECK_E2E_SECRET=credential)
    assert status == 1, "a run that proved nothing meets no expectation"
    return typ.cast("str", written[0]["detail"])


_SAVED_TRACE = Path("traces") / "incident-1-BEventually.json"
"""Where a run asked for traces writes the counterexample it found."""


def _saved_trace(traced: TracedRun) -> str:
    """Return the saved counterexample's text, failing on a run that did not pass.

    The saved file is where a leak would be committed, so a test about
    redaction reads it back rather than the in-memory trace.

    Parameters
    ----------
    traced : modelcheck_e2e_cases.TracedRun
        A run that was asked to save its traces.

    Returns
    -------
    str
        The text of the counterexample the run saved.
    """
    assert traced.result.returncode == 0, traced.result.stderr
    return (traced.model / _SAVED_TRACE).read_text(encoding="utf-8")


def _assert_no_trace_in_record(records: Path) -> None:
    """Fail when a matrix record carries a trace, which the callers rely on.

    A judgement row records the verdict and its measurements; a counterexample
    the matrix found is written to the saved-trace file instead. The credential
    assertions of the callers are therefore made on that file, and this guard
    keeps the reason sound: if the record ever starts carrying traces, the
    guard fails and the assertion has to move with it. Asserting the credential
    is absent from the record instead would pass whether or not redaction
    worked, because the record never held the trace to begin with.

    Parameters
    ----------
    records : pathlib.Path
        The JSON artefact the matrix wrote.
    """
    rows = json.loads(records.read_text(encoding="utf-8"))
    assert not any("trace" in row for row in rows), (
        "the record carries no trace, so the saved trace is where redaction shows"
    )


def test_an_unreadable_graph_reaches_the_persisted_record(tmp_path: Path) -> None:
    """A graph that cannot be read is an error in the record, with bounded evidence.

    The adapter deletes the scratch directory as the run returns, so the
    checker's transcript is the only evidence left of a run whose graph could
    not be read. The record must carry the reason and that transcript, bounded
    and with the runner's own paths and credentials replaced: the artefact is
    committed, and a path or a secret that only makes sense on the machine that
    ran it is not evidence about the model. The stub prints the credential it
    was given, so the assertion is about a value that really was in the text
    rather than one that never reached it.
    """
    inherited_value = "s3cr3t-do-not-record"

    status, written = persisted(
        tmp_path, "unreadable", MODELCHECK_E2E_SECRET=inherited_value
    )

    assert status == 1, "a run that proved nothing meets no expectation"
    assert {record["verdict"] for record in written} == {"error"}, (
        "every expectation's run is recorded as an error"
    )
    detail = typ.cast("str", written[0]["detail"])
    assert_holds(
        detail,
        {
            "the reason names the read": "the error graph could not be read",
            "the transcript is kept": "the checker printed",
            "the scratch path is replaced": "reading <scratch>/model.fizz",
            "the home path is replaced": "cache <home>/.cache/go-build",
            "the credential is replaced": "token=<redacted>",
            "the transcript is bounded and the cut stated": "first 2000 and last 2000 of",
        },
    )
    assert ".model-check" not in detail, (
        "no part of the runner path is left in the record"
    )
    assert inherited_value not in detail, "nor any inherited environment value"
    assert detail.count("x") < 5000, "so the whole runaway transcript is not kept"
    assert "\n…\n" in detail, "and the omission is marked between the kept ends"
    assert typ.cast("float", written[0]["harness_seconds"]) >= 0.0, (
        "the harness timed the run that reported no duration of its own"
    )


def test_a_malformed_graph_reaches_the_persisted_record(tmp_path: Path) -> None:
    """A graph that is readable but is not an error graph is recorded as an error.

    The text parses as JSON, so the failure happens in the decoder rather than
    the reader. The record must name the parse and keep the document, because
    the scratch directory holding it is deleted as the run returns and a
    maintainer diagnosing the checker's output needs to see what it wrote. The
    document is checker-written, so it is redacted like the transcript: a
    credential the checker wrote into it does not reach the committed record.
    """
    written_into_the_graph = "s3cr3t-in-the-graph"
    detail = _persisted_detail(tmp_path, "malformed", written_into_the_graph)

    assert_holds(
        detail,
        {
            "the reason names the parse": "the error graph could not be parsed",
            "the document is kept": "the error graph held",
            "as the checker wrote it": '{"not": "a list", "token": "<redacted>"}',
            "beside the transcript": "the checker printed",
        },
    )
    assert written_into_the_graph not in detail, (
        "the credential the checker echoed into its graph is not recorded"
    )


@pytest.mark.parametrize(
    "written_into_the_graph",
    [
        pytest.param('a"b\\c-do-not-record', id="escaped-quote-and-backslash"),
        pytest.param('a"bé-do-not-record', id="escaped-quote-and-literal-unicode"),
    ],
)
def test_an_escaped_credential_reaches_the_persisted_record_redacted(
    tmp_path: Path, written_into_the_graph: str
) -> None:
    r"""A credential a JSON writer escaped is redacted in its escaped form.

    A checker that writes its graph as JSON escapes a quote or a backslash in
    the value it writes, so the document holds ``a\\"b\\\\c`` where the
    credential is ``a"b\\c``. A value that also holds a literal non-ASCII
    character is a third shape: the quote is escaped and the character is not,
    so a needle built from either the value's bytes or one uniform spelling
    misses it. Replacing only the literal value would leave the escaped form —
    the same credential — in the record, which is where the leak would be
    committed. The record is read back here for that reason.
    """
    detail = _persisted_detail(tmp_path, "malformed", written_into_the_graph)

    assert '{"not": "a list", "token": "<redacted>"}' in detail, (
        "the escaped credential is replaced whole"
    )
    assert "do-not-record" not in detail, "and no part of it is left behind"


def test_a_go_escaped_credential_in_a_malformed_graph_is_redacted(
    tmp_path: Path,
) -> None:
    r"""A spelling only the checker's encoder produces is redacted too.

    The malformed graph is retained as the checker wrote it, and the checker is
    a Go program: its encoder writes ``<`` as ``\u003c``, a spelling no Python
    JSON writer produces. The parser's refusal quotes the *decoded* document,
    so the same credential also appears in the reason, in a third spelling.
    Both are removed before the record is written, and the record is read back
    to prove it rather than the in-memory value.
    """
    written_into_the_graph = "a<b-do-not-record"
    detail = _persisted_detail(tmp_path, "malformed", written_into_the_graph)

    assert "the error graph could not be parsed" in detail, "the reason survives"
    assert "\\u003c" not in detail, "the Go spelling of the value is replaced"
    assert "do-not-record" not in detail, (
        "and no part of the credential is left in either spelling"
    )


def test_a_one_character_credential_echoed_as_a_value_is_redacted(
    tmp_path: Path,
) -> None:
    """A flag's value is replaced where it is a value, and a count is not.

    ``CLAUDE_CODE_SESSION_ATTENDED=0`` is a boolean, not a secret, but the
    adapter cannot know that from the name. A bare ``0`` needle would rewrite
    every state count in the transcript, so the value is replaced only where a
    checker echoes it — after ``=`` — and the counts beside it survive.
    """
    echoed_flag = "0"
    detail = _persisted_detail(tmp_path, "unreadable", echoed_flag)

    assert "token=<redacted>" in detail, "the echoed value is replaced"
    assert "Valid Nodes: 4 Unique states: 4" in detail, (
        "while a count is not a value the checker was handed"
    )


@pytest.mark.parametrize(
    "credential",
    [
        pytest.param("a<b-do-not-record", id="go-escaped-less-than"),
        pytest.param('a"bé-do-not-record', id="escaped-quote-and-literal-unicode"),
    ],
)
def test_a_credential_in_a_decoded_trace_never_reaches_the_saved_evidence(
    tmp_path: Path, credential: str
) -> None:
    r"""A graph that parses is evidence in decoded form, and it is redacted too.

    Redacting the graph's text answers only for the spellings the adapter knows
    how to write. The checker is a Go program, and its encoder writes a
    less-than sign as a Unicode escape, which no Python JSON writer produces:
    a credential containing one survives the text pass and is a plain value
    once the graph is decoded. A value mixing an escaped quote with a literal
    non-ASCII character reaches the same place by a different route, because
    no one spelling of it is the value. The saved trace file is where a leak
    would be committed: a matrix judgement row records the verdict and its
    measurements rather than the trace, so the assertion is made on the file,
    on a run that really parsed a lasso.
    """
    records = tmp_path / "matrix.json"

    traced = traced_run(
        tmp_path,
        "--json",
        str(records),
        MODELCHECK_E2E_CREDENTIAL="1",
        MODELCHECK_E2E_SECRET=credential,
    )

    saved = _saved_trace(traced)
    assert "<redacted>" in saved, "the decoded value is replaced in the saved trace"
    assert "do-not-record" not in saved, "so the credential does not reach the file"
    _assert_no_trace_in_record(records)


def test_a_stopped_run_reaches_the_persisted_record_redacted(tmp_path: Path) -> None:
    """A stopped run's marker is evidence, so the record is redacted too.

    The checker prints the marker when a signal cuts the run short, and the
    line can carry the credential and the runner path it echoed around the
    interruption. The record keeps the detail, so both are replaced before it
    is written; a stopped run is not a special case that escapes redaction.
    """
    inherited_value = "s3cr3t-do-not-record"

    detail = _persisted_detail(tmp_path, "stopped", inherited_value)

    assert_holds(
        detail,
        {
            "the marker is kept": "Model checker stopped",
            "the credential is replaced": "token=<redacted>",
            "the scratch path is replaced": "at <scratch>/model.fizz",
        },
    )
    assert inherited_value not in detail, "no credential reaches the record"
    assert ".model-check" not in detail, "and no runner path either"


def test_a_one_character_credential_beside_its_label_reaches_the_record_redacted(
    tmp_path: Path,
) -> None:
    """A value the checker names rather than assigns is still a value.

    The marker line a stopped run leaves can name the variable beside its
    value, and nothing but the label marks the character as a value. The record
    is read back here because it is where a leak would be committed.
    """
    detail = _persisted_detail(tmp_path, "labelled", "x")

    assert_holds(
        detail,
        {
            "the marker is kept": "Model checker stopped",
            "the labelled value is replaced": "token <redacted>",
            "the scratch path is replaced": "at <scratch>/model.fizz",
        },
    )
    assert "token x" not in detail, "and the value is not recorded"


@pytest.mark.parametrize("one_character", ["x", "#"])
def test_a_decoded_string_containing_a_one_character_credential_is_withheld(
    tmp_path: Path, one_character: str
) -> None:
    """A state built from a one-character value is withheld from the trace.

    The graph's text holds the character inside a longer word, which no
    position rule can recognize, and the decoded string is what the saved trace
    carries. The string is withheld rather than recorded with the character
    recoverable from it, whether the character is a letter or punctuation.
    """
    records = tmp_path / "matrix.json"
    embedded = f"prefix{one_character}"

    traced = traced_run(
        tmp_path,
        "--json",
        str(records),
        MODELCHECK_E2E_CREDENTIAL="1",
        MODELCHECK_E2E_EMBED="1",
        MODELCHECK_E2E_SECRET=one_character,
    )

    saved = _saved_trace(traced)
    assert WITHHELD in saved, "the string is withheld in the saved trace"
    assert embedded not in saved, "so the character is not recoverable"
    _assert_no_trace_in_record(records)


def test_a_one_character_credential_embedded_in_a_graph_reaches_the_record(
    tmp_path: Path,
) -> None:
    """The one residual the policy accepts is pinned here, not merely claimed.

    A credential of two or more characters is withheld from the record when a
    replacement missed it, because the value can be recovered from the text. A
    single character cannot be a needle: it occurs in every state count, so
    withholding the texts it survives in would withhold nearly every diagnostic
    a maintainer needs. The record therefore carries the word the checker hid
    the character in, together with the diagnosis that makes the record worth
    reading. What it cannot do is tell that one character from the prose around
    it, and this test is where that limit is stated rather than where it is
    closed.
    """
    detail = _persisted_detail(tmp_path, "malformed-embedded", "#")

    assert "the error graph could not be parsed" in detail, "the reason survives"
    assert "prefix#" in detail, (
        "the character stays inside the word, which is the accepted residual"
    )


def test_an_equivalent_escape_spelling_is_withheld_from_the_record(
    tmp_path: Path,
) -> None:
    r"""A spelling no needle enumerates is withheld rather than recorded.

    ``a<b-do-not-record`` can be written with every character escaped, which
    is valid JSON and matches neither the value nor the Go spelling the needles
    know. A reader can still recover the value from it, so the record must not
    carry that document: the capture is withheld, and the marker says why. The
    scrubbing runs on each capture before it is cut, so the diagnosis beside it
    — which says what the parser objected to — is kept rather than lost with
    the document.
    """
    written_into_the_graph = "a<b-do-not-record"
    detail = _persisted_detail(tmp_path, "malformed-escaped", written_into_the_graph)

    assert "the error graph could not be parsed" in detail, "the reason survives"
    assert WITHHELD in detail, "the document is withheld rather than recorded"
    assert "do-not-record" not in detail, "so the value is not recoverable"
