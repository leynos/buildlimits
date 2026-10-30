"""The TOML adapter is the only reader of a model directory's file layout.

The adapter is tested against real files written into a pytest scratch
directory rather than against a parsed document, because what it owns is the
*translation*: a table becomes a ``Configuration``, a typo becomes a
``MaterializeError`` naming the configuration, and an absent optional table
becomes a default rather than a failure. Testing it through the CLI would say
only that some configuration ran, not which one the file produced.

Every failure test asserts the message names the configuration. A reader who
is told only that a TOML file is invalid has to find the table themselves, and
a model with twenty variants is where that costs the most.
"""

import typing as typ

import pytest

from modelcheck import domain
from modelcheck.adapters.configuration import TomlConfigurations

if typ.TYPE_CHECKING:
    from pathlib import Path

MODEL = """\
action Init:  # maps: buildlimits.admission.Admission.submit
  n = 0
"""
"""The source a model directory holds in these tests."""


def _directory(tmp_path: Path, configurations: str = "") -> Path:
    """Write a model directory and return it.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The scratch directory pytest supplied.
    configurations : str
        The ``configurations.toml`` contents, or nothing for no file.

    Returns
    -------
    pathlib.Path
        The directory, holding the model and the configuration file.
    """
    (tmp_path / "admission.fizz").write_text(MODEL, encoding="utf-8")
    if configurations:
        (tmp_path / "configurations.toml").write_text(configurations, encoding="utf-8")
    return tmp_path


def _only(directory: Path) -> domain.Configuration:
    """Return the sole configuration a directory declares.

    Parameters
    ----------
    directory : pathlib.Path
        The model directory.

    Returns
    -------
    domain.Configuration
        The one configuration.
    """
    found = TomlConfigurations(directory, "routine").configurations()
    assert len(found) == 1, "the directory declares one configuration"
    return found[0]


def _refusal(tmp_path: Path, configurations: str, match: str | None = None) -> str:
    """Write a configuration file and return the refusal reading it earns.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The scratch directory pytest supplied.
    configurations : str
        The ``configurations.toml`` contents to write.
    match : str | None
        A pattern the message must match, or ``None`` to accept any refusal.

    Returns
    -------
    str
        The message the adapter raised, for a test that asserts on more than
        the pattern.
    """
    directory = _directory(tmp_path, configurations)

    with pytest.raises(domain.MaterializeError, match=match) as raised:
        TomlConfigurations(directory).configurations()

    return str(raised.value)


def test_the_source_is_the_model_text(tmp_path: Path) -> None:
    """The adapter publishes the model's text, unchanged."""
    directory = _directory(tmp_path)

    assert TomlConfigurations(directory).source_text() == MODEL, "the text, unchanged"


def test_a_directory_without_a_model_is_refused(tmp_path: Path) -> None:
    """A model directory with no ``.fizz`` file is a configuration defect.

    A run over no source would compile nothing and report a pass over an empty
    state space, so the adapter refuses rather than returning an empty string.
    """
    with pytest.raises(domain.MaterializeError, match=r"holds no admission\.fizz"):
        TomlConfigurations(tmp_path).source_text()


def test_every_declared_field_reaches_the_configuration(tmp_path: Path) -> None:
    """The table becomes the domain's value, field by field."""
    directory = _directory(
        tmp_path,
        """
[configurations.head]
min_states = 1000
holds = ["S1", "S2"]
reachable = ["StopIssued"]
fails_as = { L3 = "lasso" }

[configurations.head.fairness]
F1 = "weak"

[configurations.head.constants]
CAPACITY = 3
STRICT = true
NAME = "queue"

[configurations.head.options]
max_actions = 400
deadlock_detection = true
""",
    )

    configuration = _only(directory)

    assert configuration.name == "head", "the table's name is the configuration"
    assert configuration.min_states == 1000, "the floor is read as a count"
    expected = [
        domain.Holds("S1"),
        domain.Holds("S2"),
        domain.Reachable("StopIssued"),
        domain.FailsAs("L3", domain.CounterexampleKind.LASSO),
    ]
    assert list(configuration.expectations) == expected, (
        "the expectations keep the file's order"
    )
    assert configuration.fairness == {"F1": "weak"}, "the fairness table is read"
    assert configuration.constants == {
        "CAPACITY": "3",
        "STRICT": "true",
        "NAME": "queue",
    }, "constants are rendered as the model spells them"
    assert configuration.options == {"max_actions": 400, "deadlock_detection": True}, (
        "the options table is read"
    )


def test_a_boolean_constant_is_rendered_as_the_model_spells_it(
    tmp_path: Path,
) -> None:
    """TOML's ``true`` substitutes as the model's ``true``, not Python's ``True``.

    A constant is substituted into the model's own source, where ``True`` does
    not compile. Rendering it here rather than in the materializer keeps that
    spelling rule beside the format that needs it.
    """
    directory = _directory(
        tmp_path,
        """
[configurations.head.constants]
DEADLOCK = false
""",
    )

    assert _only(directory).constants == {"DEADLOCK": "false"}, (
        "TOML's false, not Python's"
    )


def test_a_configuration_may_declare_nothing(tmp_path: Path) -> None:
    """An empty table is a configuration that expects nothing, not an error.

    ``Configurations`` treats an empty table as a variant with no expectations.
    Refusing it here would make the *file's* shape a requirement the domain
    never stated; the service is where an empty *matrix* is refused.
    """
    configuration = _only(_directory(tmp_path, "[configurations.head]\n"))

    assert configuration == domain.Configuration("head", {}, {}, {}, 0, ()), (
        "an empty table expects nothing"
    )


def test_a_missing_file_declares_nothing(tmp_path: Path) -> None:
    """A model with no configuration file has no configurations and no exclusions."""
    directory = _directory(tmp_path)
    source = TomlConfigurations(directory)

    assert not source.configurations(), "no file declares no configurations"
    assert not source.exclusions(), "and no exclusions either"


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param("[configurations.head\n", "is not valid TOML", id="unparsable"),
        pytest.param('configurations = "head"\n', "not a table", id="wrong-shape"),
    ],
)
def test_a_document_that_cannot_be_read_is_refused(
    tmp_path: Path, document: str, expected: str
) -> None:
    """A document defect is refused, naming the file and what is wrong with it.

    Both cases are the file failing as a whole rather than one field inside it:
    a file that does not parse, and a top-level key whose value is a scalar
    where the adapter reads a table. Refusing either is what keeps a typo from
    being read as a model that declares nothing.
    """
    message = _refusal(tmp_path, document, expected)

    assert "configurations.toml" in message, "the file a reader must open"


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ('min_states = "many"', "min_states that is not a count"),
        ("min_states = -1", "min_states that is not a count"),
        ("min_states = true", "min_states that is not a count"),
        ('holds = "S1"', "'holds' that is not a list"),
        ("holds = 3", "'holds' that is not a list"),
        ("reachable = [1]", "'reachable' entry that is not a name"),
        ('fails_as = "L3"', "'fails_as' that is not a table"),
        ('fails_as = { L3 = "spiral" }', "is not one of"),
        ("fairness = 3", "'fairness' that is not a table"),
        ('constants = "x"', "'constants' that is not a table"),
        ("options = 3", "an 'options' that is not a table"),
    ],
)
def test_a_malformed_field_is_refused_by_name(
    tmp_path: Path, table: str, expected: str
) -> None:
    """Each defect is reported, and names the configuration to look at."""
    message = _refusal(tmp_path, f"[configurations.head]\n{table}\n")

    assert "'head'" in message, "the configuration is named"
    assert expected in message, "and the defect is named"


def test_a_string_field_is_not_iterated_character_by_character(
    tmp_path: Path,
) -> None:
    """A scalar where a list belongs is one error, not one per character.

    Casting the field rather than checking it would accept ``holds = "S1"`` and
    produce expectations called ``S`` and ``1``: a typo that reads as a model
    with two properties nobody declared. The refusal is what makes the typo
    visible.
    """
    message = _refusal(tmp_path, '[configurations.head]\nholds = "S1"\n', "not a list")

    assert "'S'" not in message, "the scalar was refused, not iterated"


@pytest.mark.parametrize("level", ["sometimes", "Fair", ""])
def test_a_fairness_level_must_be_one_of_three(tmp_path: Path, level: str) -> None:
    """An unknown level cannot be compared with what the checker honours."""
    directory = _directory(
        tmp_path,
        f'[configurations.head.fairness]\nF1 = "{level}"\n',
    )

    with pytest.raises(domain.MaterializeError, match="fairness tag 'F1'"):
        TomlConfigurations(directory).configurations()


def test_configurations_keep_their_file_order(tmp_path: Path) -> None:
    """The report shows configurations in the order the file declares them."""
    directory = _directory(
        tmp_path,
        "[configurations.zeta]\n[configurations.alpha]\n",
    )

    names = [item.name for item in TomlConfigurations(directory).configurations()]

    assert names == ["zeta", "alpha"], "the file's order is kept"


def test_exclusions_carry_their_reason(tmp_path: Path) -> None:
    """An exclusion is a considered gap, and the reason travels with it."""
    directory = _directory(
        tmp_path,
        """
[exclusions]
"buildlimits.effects.Pause" = "modelled as an unobservable effect"
""",
    )

    assert TomlConfigurations(directory).exclusions() == {
        "buildlimits.effects.Pause": "modelled as an unobservable effect"
    }, "the reason travels with the exclusion"


def test_the_document_is_read_once(tmp_path: Path) -> None:
    """A second read does not go back to the filesystem.

    The file is rewritten after the first read; a cached document reports the
    original, which is what makes ``configurations`` and ``exclusions`` one
    consistent snapshot of a file a concurrent edit could otherwise split.
    """
    directory = _directory(tmp_path, "[configurations.head]\n")
    source = TomlConfigurations(directory)
    source.configurations()
    (directory / "configurations.toml").write_text(
        "[configurations.other]\n", encoding="utf-8"
    )

    assert [item.name for item in source.configurations()] == ["head"], (
        "the second read is the cached document"
    )
