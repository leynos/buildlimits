"""The evidence a failed run keeps is read and refused deliberately.

A counterexample is only as good as the evidence beside it. The adapter reads
the checker's error graph from the directory it named, tells an absent graph
from one it could not read, and refuses a graph it cannot decode or parse
rather than guessing a trace kind from it. Each of those decisions changes what
a maintainer is told, so each is asserted here against a stub that produces the
failure rather than against a hand-built value.

The text beside the graph is bounded before it becomes a diagnostic, because it
is checker-written and its length is the checker's choice. The same is true of
the line an error or a stopped run is read from, which is persisted whole
otherwise. Bounding is never the first thing done to it: a cut through a
credential would keep a fragment of it that the survival check cannot
recognize, so the whole capture is scrubbed first and two of the tests here
place a credential exactly where the cut falls.

What must be *replaced* rather than bounded — runner paths and credentials — is
asserted in :mod:`tests.test_modelcheck_fizzbee_redaction`, so each module is
about one question. The stub launcher and the specification it runs come from
:mod:`tests.fizzbee_process_stubs`; the run around each test's own error graph
comes from :mod:`tests.modelcheck_evidence_cases`.
"""

from pathlib import Path

import pytest
from fizzbee_process_stubs import spec, stub
from modelcheck_evidence_cases import graph_run, run_stub

from modelcheck import domain
from modelcheck.adapters.fizzbee_evidence import _TRANSCRIPT_LIMIT
from modelcheck.adapters.fizzbee_process import FizzbeeChecker

_CREDENTIAL = "s3cr3t-do-not-record"
"""A credential long enough to be replaced wherever it occurs."""

_HEAD = _TRANSCRIPT_LIMIT // 2
"""How much of a cut capture is kept from its start; the rest comes from its end."""

_STRADDLING = "x" * (_HEAD - len("<redacted>"))
"""Filler that puts a credential across the kept head's boundary.

The scrub replaces a credential with a ten-character placeholder before the
cut, so a value beginning here ends exactly where the head does. In the text
the checker wrote it straddles the cut: a bounding pass that ran first would
keep ten characters of the credential. Ten is chosen so the placeholder still
fits inside the kept head, which lets a test assert the replacement happened
rather than only that the fragment is gone.
"""

_BEYOND = "x" * (_TRANSCRIPT_LIMIT - _HEAD + len("<redacted>"))
"""Filler that keeps the capture longer than the limit.

A straddling credential is short, so without text after it the capture would
fit and no cut would be taken at all.
"""


def test_an_error_graph_is_read_from_the_named_output_directory() -> None:
    """A graph that is not there is absent, not an exception.

    A run that passed, or a counterexample kind that carries no graph, writes no
    file. Absence is ordinary, so it must not become an error that hides the
    verdict the output already gave.
    """
    from modelcheck.adapters.fizzbee_process import _graph

    assert _graph(Path("/nonexistent")) is None, "a missing graph is absent, not raised"


def test_an_unreadable_error_graph_is_reported_rather_than_absent(
    tmp_path: Path,
) -> None:
    """A graph that exists but cannot be read is an error, not a missing graph.

    Only absence is ordinary: a permission or I/O failure means the evidence the
    counterexample needs is missing for a reason the run report must name.
    Reporting it as absent would turn a broken read into a pass, or into a
    finding it cannot support.
    """
    from modelcheck.adapters.fizzbee_process import _graph

    (tmp_path / "error-graph.json").mkdir()

    with pytest.raises(IsADirectoryError):
        _graph(tmp_path)


def test_a_run_whose_graph_cannot_be_read_is_a_run_that_proved_nothing(
    tmp_path: Path,
) -> None:
    """An unreadable graph is reported as an error, not as a counterexample.

    The adapter is the only place that can tell absence from a failed read, and
    the difference decides whether a violation is reported with evidence or
    with a trace kind nobody established. A directory where the graph should be
    is the cheapest unreadable graph to produce.
    """
    result = graph_run(tmp_path, "mkdir -p out/error-graph.json\n")

    assert isinstance(result, domain.Errored), "the run proved nothing"
    assert "could not be read" in result.detail, "and the reason names the read"
    assert "the checker printed:\nValid Nodes: 4 Unique states: 4\n" in result.detail, (
        "the transcript is kept because the scratch directory holding it is deleted"
    )


@pytest.mark.parametrize(
    ("prepare", "reason"),
    [
        (
            "mkdir -p out\nprintf '\\xff\\xfe' > out/error-graph.json\n",
            "could not be read",
        ),
        (
            "mkdir -p out\nprintf '{not json' > out/error-graph.json\n",
            "could not be parsed",
        ),
    ],
    ids=["undecodable", "unparsable"],
)
def test_a_graph_the_adapter_cannot_turn_into_a_trace_proved_nothing(
    tmp_path: Path, prepare: str, reason: str
) -> None:
    """A graph that cannot become a trace is an error, not a missing graph.

    Two stages can refuse it, and both must refuse at the same boundary. Bytes
    nobody can decode fail while the file is read, arriving as a
    ``UnicodeDecodeError`` rather than an ``OSError``. Text that decodes but
    parses as nothing fails in the parser instead. Either way the violation's
    path cannot be read, so the run proved nothing and no shape-specific
    expectation may be met by it; letting either escape as a traceback would
    turn a broken graph into a crash rather than a report.
    """
    result = graph_run(tmp_path, prepare)

    assert isinstance(result, domain.Errored), "the run proved nothing"
    assert reason in result.detail, f"and the reason names the stage: {reason}"


def test_a_deciding_error_line_is_bounded(tmp_path: Path) -> None:
    """The line an error verdict is read from is bounded like a transcript.

    The checker writes that line, so its length is the checker's choice. A
    run whose ``Error:`` line carried a whole dump would otherwise put the dump
    into the persisted detail, which is bounded for the transcript's own
    reasons.
    """
    fizz = stub(
        tmp_path,
        "#!/usr/bin/env bash\nprintf 'Error: %s\\n' \"$(printf 'x%.0s' {1..5000})\"\n",
    )

    result = FizzbeeChecker(fizz).check(spec())

    assert isinstance(result, domain.Errored), "an error line proves nothing"
    assert result.detail.startswith("Error: "), "the cause is still named"
    assert "first 2000 and last 2000 of 5007 characters kept" in result.detail, (
        "and the cut is stated"
    )
    assert len(result.detail) < 5000, "so the whole line is not kept"


def test_a_deciding_stopped_line_is_bounded(tmp_path: Path) -> None:
    """The marker a cut-short run is read from is bounded for the same reason.

    A stopped run's detail is the marker line itself, and it is persisted with
    the record. The marker is checker-written text, so it gets the same limit
    the transcript does rather than being trusted to be short.
    """
    fizz = stub(
        tmp_path,
        "#!/usr/bin/env bash\n"
        "printf 'Model checker stopped %s\\n' \"$(printf 'y%.0s' {1..5000})\"\n",
    )

    result = FizzbeeChecker(fizz).check(spec())

    assert isinstance(result, domain.Stopped), "the marker names a stopped run"
    assert result.detail.startswith("Model checker stopped "), "the shape is kept"
    assert "first 2000 and last 2000 of" in result.detail, "and the cut is stated"
    assert len(result.detail) < 5000, "so the whole line is not kept"


def test_a_credential_the_transcript_cut_would_split_is_scrubbed_whole(
    tmp_path: Path,
) -> None:
    """A credential is removed before the transcript is cut, not after it.

    The head of the transcript is kept, and a credential placed where that head
    ends would leave part of itself in the kept text. The survival check cannot
    find a fragment, so the whole capture has to be scrubbed while it is still
    one string — which is what the adapter does, and what the run's environment
    here makes observable.
    """
    result = run_stub(
        tmp_path,
        "#!/usr/bin/env bash\n"
        f"printf '%s%s%s\\n' '{_STRADDLING}' '{_CREDENTIAL}' '{_BEYOND}'\n",
        {"RUNNER_TOKEN": _CREDENTIAL},
    )

    assert isinstance(result, domain.Errored), "the run printed no verdict"
    assert _CREDENTIAL[:5] not in result.detail, "no fragment of it is kept"
    assert "<redacted>" in result.detail, "the value was replaced, not dropped"
    assert "first 2000 and last 2000 of 4011 characters kept" in result.detail, (
        "and the cut is stated"
    )


def test_a_credential_the_graph_cut_would_split_is_scrubbed_whole(
    tmp_path: Path,
) -> None:
    """A malformed graph is scrubbed as one document before it is bounded.

    The refused graph is kept as evidence, and it is longer than the limit for
    the same reason a transcript is: the checker wrote it. Scrubbing each
    capture before the cut is what keeps half a credential out of the record
    when the value sits across the boundary.
    """
    result = graph_run(
        tmp_path,
        "mkdir -p out\n"
        f"printf '%s%s%s}}' '{_STRADDLING}' '{_CREDENTIAL}' '{_BEYOND}' "
        "> out/error-graph.json\n",
        {"RUNNER_TOKEN": _CREDENTIAL},
    )

    assert isinstance(result, domain.Errored), "the graph proved nothing"
    assert _CREDENTIAL[:5] not in result.detail, "no fragment of it is kept"
    assert "<redacted>" in result.detail, "the value was replaced, not dropped"
    assert "first 2000 and last 2000 of 4011 characters kept" in result.detail, (
        "and the cut is stated"
    )
