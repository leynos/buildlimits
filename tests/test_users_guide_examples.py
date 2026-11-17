"""The user's guide's tested examples must name what the code reads."""

from pathlib import Path

from buildlimits.journal_policy import ROUTINE_LINES_VARIABLE
from buildlimits.predict import benchmark_cli, dataset_cli

GUIDE = Path(__file__).parents[1] / "docs" / "users-guide.md"
DROP_IN = "buildlimits-routine-lines-drop-in"
PREDICT_EXAMPLE = "buildlimits-predict-commands"


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


def test_the_predict_example_names_the_default_paths_the_commands_use() -> None:
    """The guide's explicit paths are the defaults the commands would apply.

    A change to either default would silently make the published run write
    somewhere other than the paths a reader copies, so the example pins them.
    """
    body = _marked_example(PREDICT_EXAMPLE, "bash")

    assert f"--output {dataset_cli.DEFAULT_OUTPUT}" in body, (
        "the dataset example writes the snapshot default"
    )
    assert f"--snapshot {benchmark_cli.DEFAULT_SNAPSHOT}" in body, (
        "the benchmark example reads the snapshot default"
    )
    assert f"--output {benchmark_cli.DEFAULT_OUTPUT_DIR}" in body, (
        "the benchmark example writes the report directory default"
    )
