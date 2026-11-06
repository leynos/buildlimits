"""The evidence a failed run persists is read back because it is committed.

These tests run the real command line into a written record and then read that
record. The adapter deletes the scratch directory as the run returns, so the
persisted detail is the only place the checker's transcript and its error graph
survive; what it carries is what a maintainer will have. That makes the record
the right place to assert on bounding and redaction: a test against an
in-process value could pass while the recorded text still carried a runner path
or a credential.

The stub's modes are described in :mod:`tests.fixtures.modelcheck`; the helpers
that run it are shared with :mod:`tests.test_modelcheck_cli_e2e`.
"""

import typing as typ
from pathlib import Path

from modelcheck_e2e_cases import assert_holds, persisted


def _malformed_detail(tmp_path: Path, credential: str) -> str:
    """Run the malformed-graph mode and return the detail it persisted.

    Both malformed-graph tests need the same three things before they can
    assert anything: a run whose credential is the one under test, the status
    that says the run proved nothing, and the detail the record kept. Sharing
    them keeps each test's assertions the only thing that differs.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The scratch directory the run owns.
    credential : str
        The value the stub is given as ``MODELCHECK_E2E_SECRET``, which it
        writes into the graph it produces.

    Returns
    -------
    str
        The persisted detail of the first record.
    """
    status, written = persisted(tmp_path, "malformed", MODELCHECK_E2E_SECRET=credential)
    assert status == 1, "a run that proved nothing meets no expectation"
    return typ.cast("str", written[0]["detail"])


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
            "the transcript is bounded and the cut stated": "first 4000 of",
        },
    )
    assert ".model-check" not in detail, (
        "no part of the runner path is left in the record"
    )
    assert inherited_value not in detail, "nor any inherited environment value"
    assert detail.count("x") < 5000, "so the whole runaway transcript is not kept"
    assert detail.endswith("…"), "and the omission is marked"
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
    detail = _malformed_detail(tmp_path, written_into_the_graph)

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


def test_an_escaped_credential_reaches_the_persisted_record_redacted(
    tmp_path: Path,
) -> None:
    r"""A credential a JSON writer escaped is redacted in its escaped form.

    A checker that writes its graph as JSON escapes a quote or a backslash in
    the value it writes, so the document holds ``a\\"b\\\\c`` where the
    credential is ``a"b\\c``. Replacing only the literal value would leave the
    escaped form — the same credential — in the record, which is where the leak
    would be committed. The record is read back here for that reason.
    """
    written_into_the_graph = 'a"b\\c-do-not-record'
    detail = _malformed_detail(tmp_path, written_into_the_graph)

    assert '{"not": "a list", "token": "<redacted>"}' in detail, (
        "the escaped credential is replaced whole"
    )
    assert "do-not-record" not in detail, "and no part of it is left behind"
