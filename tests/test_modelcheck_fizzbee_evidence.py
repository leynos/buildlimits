"""The evidence a failed run keeps is bounded and says so.

A checker transcript can be arbitrarily long, and a diagnostic that embedded
all of it would be unreadable and would put a runaway run's whole output into
the JSON artefact. The retention is a fixed prefix, and the cut is stated
rather than silent so a reader knows the evidence was truncated. The module's
doctests pin the ordinary shapes; this module pins the bound itself.
"""

from pathlib import Path

from modelcheck.adapters.fizzbee_evidence import redacted, with_evidence


def test_a_long_capture_is_cut_at_the_stated_limit() -> None:
    """Only the first part of a runaway transcript is kept."""
    rendered = with_evidence("no graph", [("printed", "x" * 5000)])

    assert "first 4000 of 5000 characters" in rendered, "the cut is stated"
    assert rendered.count("x") == 4000, "and the prefix is what was kept"
    assert rendered.endswith("…"), "with the omission marked"


def test_a_capture_within_the_limit_is_kept_whole() -> None:
    """A short transcript is not truncated, and carries no note."""
    rendered = with_evidence("no graph", [("printed", "x" * 4000)])

    assert rendered.count("x") == 4000, "the whole capture is kept"
    assert "characters" not in rendered, "and no cut is claimed"


def test_the_scratch_directory_is_replaced_however_it_is_spelled(
    tmp_path: Path,
) -> None:
    """Both the given path and its resolved form are redacted.

    A diagnostic may carry the directory as the adapter was handed it or as the
    operating system resolves it, so replacing only one spelling would leave a
    runner path in the record whenever the two differ.
    """
    directory = tmp_path / "run-1"
    directory.mkdir()

    rendered = redacted(f"open {directory} and {directory.resolve()} failed", directory)

    assert rendered == "open <scratch> and <scratch> failed", "both are replaced"
    assert str(tmp_path) not in rendered, "and no part of the runner path remains"
