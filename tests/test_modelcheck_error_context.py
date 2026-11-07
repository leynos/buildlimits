"""What a failed run says about where it failed.

The parser has one job beyond classification: keep enough of what the checker
printed for a maintainer to act on. Two failures are easy to lose. A compile
failure is reported as the offending source position followed by a one-line
summary, and the summary alone does not say which line was refused. A crash
inside the checker may print no ``Error:`` line at all, leaving an exit status
as the only fact unless the transcript is kept beside it.

The tests are separate from ``test_modelcheck_fizzbee_output``'s marker sweep
because they are about the evidence a failed run carries rather than about the
classification of a marker, and that module is at the repository's line limit.
"""

from modelcheck import domain
from modelcheck.adapters import fizzbee_output


def test_an_error_line_keeps_the_line_that_says_where_it_happened() -> None:
    """The summary line alone does not let a maintainer find the failure.

    The checker reports a parse failure as the offending source position and
    then a summary. Quoting only ``Error: Compilation failed`` leaves the
    report unable to say which line of the model was refused, which is the
    first thing a reader needs. The two lines are kept together.
    """
    result = fizzbee_output.parse_output(
        "line 17:0 mismatched input 'assertion' expecting <EOF>\n"
        "Error: Compilation failed\nLogs at out/log\n",
        1,
        None,
    )

    assert isinstance(result, domain.Errored), "a compile failure proves nothing"
    assert result.detail == (
        "line 17:0 mismatched input 'assertion' expecting <EOF>\n"
        "Error: Compilation failed"
    ), "the position is kept beside the summary"


def test_a_non_zero_status_without_an_error_line_keeps_the_transcript() -> None:
    """A status alone cannot be diagnosed, so what the checker printed is kept.

    The checker can fail without printing an ``Error:`` line — a crash inside
    the tool, or a change to its output. Reporting only the exit status would
    put a number into the record where the reason was available, so the
    transcript is attached as bounded evidence.
    """
    result = fizzbee_output.parse_output(
        "Valid Nodes: 2 Unique states: 2\nunexpected shutdown\n", 3, None
    )

    assert isinstance(result, domain.Errored), "a non-zero exit proves nothing"
    assert "the checker exited with status 3" in result.detail, "the status is named"
    assert "unexpected shutdown" in result.detail, (
        "and the output that explains it is kept"
    )
