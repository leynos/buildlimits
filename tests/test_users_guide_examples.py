"""The user's guide's tested examples must name what the supervisor reads."""

from pathlib import Path

from buildlimits.journal_policy import ROUTINE_LINES_VARIABLE

GUIDE = Path(__file__).parents[1] / "docs" / "users-guide.md"
DROP_IN = "buildlimits-routine-lines-drop-in"


def _marked_example(identifier: str, language: str) -> str:
    """Load one marked example from the shipped guide.

    Parameters
    ----------
    identifier : str
        The stable identifier in the example's marker comment.
    language : str
        The fence language the marker must be followed by.

    Returns
    -------
    str
        The example's body, without its fence lines.
    """
    text = GUIDE.read_text(encoding="utf-8")
    marker = f"<!-- tested-example: {identifier} -->"
    assert text.count(marker) == 1, (
        "each tested example is identified by exactly one marker, so a "
        "duplicate cannot hide behind the first one"
    )
    start = text.index(marker) + len(marker)
    opening = f"\n```{language}\n"
    assert text.startswith(opening, start), (
        "the marker is followed immediately by its fenced example"
    )
    body, _fence, _rest = text[start + len(opening) :].partition("\n```")
    return body


def test_the_routine_line_drop_in_sets_the_switch_the_supervisor_reads() -> None:
    """The guide's drop-in names the start-up variable that turns lines on."""
    body = _marked_example(DROP_IN, "ini")
    assert body.startswith("[Service]\nEnvironment="), (
        "the example is a user-unit drop-in"
    )
    variable, _equals, value = body.partition("Environment=")[2].partition("=")
    assert variable == ROUTINE_LINES_VARIABLE, (
        "the guide names the variable the supervisor reads"
    )
    assert value == "1", "the example requests the routine lines"
