"""The user's guide's tested examples must name what the code reads."""

import datetime as dt
from pathlib import Path

from outcome_retention_helpers import ROHGA_MAX_BYTES

from buildlimits import config, outcome_archive_cli
from buildlimits.journal_policy import ROUTINE_LINES_VARIABLE
from buildlimits.outcome_record_encode import header_floor_bytes
from buildlimits.outcome_record_format import MAX_COMPACT_BYTES
from buildlimits.outcome_retention import policy_from_settings
from buildlimits.predict import benchmark_cli, dataset_cli

GUIDE = Path(__file__).parents[1] / "docs" / "users-guide.md"
DROP_IN = "buildlimits-routine-lines-drop-in"
PREDICT_EXAMPLE = "buildlimits-predict-commands"
ARCHIVE_SERVICE = "buildlimits-outcome-archive-service"
ARCHIVE_TIMER = "buildlimits-outcome-archive-timer"
ARCHIVE_ENABLE = "buildlimits-outcome-archive-enable"
ARCHIVE_COMMANDS = "buildlimits-outcome-archive-commands"
RETENTION_EXAMPLE = "buildlimits-cache-outcomes-retention"


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


def _ini_values(body: str) -> dict[str, tuple[str, ...]]:
    """Return every ``Key=Value`` pair in an INI example, grouped by key.

    Returns
    -------
    dict[str, tuple[str, ...]]
        Each key's values in the order written, so a duplicated key is visible
        rather than silently collapsed.
    """
    values: dict[str, list[str]] = {}
    for line in body.splitlines():
        key, equals, value = line.partition("=")
        if equals and not line.lstrip().startswith(("#", ";")):
            values.setdefault(key.strip(), []).append(value.strip())
    return {key: tuple(items) for key, items in values.items()}


def test_the_retention_example_states_the_policy_it_documents(
    tmp_path: Path,
) -> None:
    """The guide's copyable keys are the policy the archiver applies.

    A reader copies this block into a host's ``supervisor.toml``, so the file
    it produces must load as the thirty-day, fifteen-gigabyte policy with no
    count cap. Loading the published example rather than a copy keeps the
    documented text and the tested text the same one.
    """
    path = tmp_path / "supervisor.toml"
    path.write_text(_marked_example(RETENTION_EXAMPLE, "toml"), encoding="utf-8")

    policy = policy_from_settings(
        config.load(path),
        header_floor=header_floor_bytes(),
        segment_limit=MAX_COMPACT_BYTES,
    )

    assert policy.max_age == dt.timedelta(days=30), "the example asks for 30 days"
    assert policy.max_bytes == ROHGA_MAX_BYTES, "and fifteen decimal gigabytes"
    assert policy.max_records is None, "with no count cap to truncate history"
    assert policy.is_segmented is True, "the budget rotates the active file"


def test_the_archive_service_runs_a_subcommand_the_command_accepts() -> None:
    """The unit's ExecStart is a command line this package's parser accepts."""
    values = _ini_values(_marked_example(ARCHIVE_SERVICE, "ini"))
    (exec_start,) = values["ExecStart"]
    program, *arguments = exec_start.split()

    assert program.endswith("/build-limits-outcome-archive"), (
        "the unit runs the archiver this guide documents"
    )
    outcome_archive_cli._build_parser().parse_args(arguments)
    assert values["Type"] == ("oneshot",), "a oneshot unit exits with the run"
    assert values["MemoryMax"] == ("512M",), "the unit bounds the peak it was sized for"
    assert values["TimeoutStartSec"] == ("2h",), "the unit allows for a backlog run"


def test_the_archive_timer_is_daily_and_persistent() -> None:
    """A missed daily firing is caught up at the next start."""
    values = _ini_values(_marked_example(ARCHIVE_TIMER, "ini"))

    assert values["OnCalendar"] == ("daily",), "the guide schedules a daily run"
    assert values["Persistent"] == ("true",), "a missed firing still runs"
    assert values["WantedBy"] == ("timers.target",), "enabling the timer starts it"


def test_the_archive_enable_example_names_the_timer_it_defines() -> None:
    """The enable command is the timer unit the guide installs."""
    commands = _example_commands(_marked_example(ARCHIVE_ENABLE, "bash"))
    timer = _ini_values(_marked_example(ARCHIVE_TIMER, "ini"))

    assert timer["WantedBy"] == ("timers.target",), "the timer is installable"
    assert commands["systemctl"] == (
        "--user",
        "enable",
        "--now",
        "buildlimits-outcome-archive.timer",
    ), "the unit enabled is the one the previous example writes"


def test_the_archive_command_example_uses_subcommands_the_parser_accepts() -> None:
    """Every documented invocation is one the command actually accepts."""
    lines = [
        line.split()
        for line in _marked_example(ARCHIVE_COMMANDS, "bash").splitlines()
        if line.strip()
    ]
    parser = outcome_archive_cli._build_parser()
    invocations = [tuple(line[1:]) for line in lines]

    assert {line[0] for line in lines} == {"build-limits-outcome-archive"}, (
        "every line runs the documented command"
    )
    for arguments in invocations:
        parser.parse_args(list(arguments))
    assert invocations == [("inspect",), ("run", "--dry-run"), ("run",)], (
        "the guide shows the plan, a dry run and the real run"
    )
