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


def _example_commands(body: str) -> dict[str, tuple[str, ...]]:
    """Return each command's arguments, keyed by the program that runs them.

    A trailing backslash continues a command on the next line, so the lines are
    joined before they are split. Reading one command's arguments as a whole is
    what stops a longer path from satisfying a shorter one's assertion.

    Returns
    -------
    dict[str, tuple[str, ...]]
        The arguments of each command in the example, in the order written.
    """
    joined = body.replace("\\\n", " ")
    commands: dict[str, tuple[str, ...]] = {}
    for line in joined.splitlines():
        arguments = tuple(line.split())
        if arguments:
            commands[arguments[0]] = arguments[1:]
    return commands


def test_the_predict_example_names_the_default_paths_the_commands_use() -> None:
    """The guide's explicit paths are the defaults the commands would apply.

    A change to either default would silently make the published run write
    somewhere other than the paths a reader copies, so the example pins them.
    Each command is compared as a whole, so the dataset command's longer output
    path cannot stand in for the benchmark command's shorter one.
    """
    commands = _example_commands(_marked_example(PREDICT_EXAMPLE, "bash"))

    assert commands["build-limits-predict-dataset"] == (
        "--output",
        str(dataset_cli.DEFAULT_OUTPUT),
    ), "the dataset example writes the snapshot default"
    assert commands["build-limits-predict-benchmark"] == (
        "--snapshot",
        str(benchmark_cli.DEFAULT_SNAPSHOT),
        "--output",
        str(benchmark_cli.DEFAULT_OUTPUT_DIR),
    ), "the benchmark example reads the snapshot and writes the report defaults"
