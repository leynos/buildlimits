"""Keep isolated quality tools aligned with their development dependencies."""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
MAKEFILE = ROOT / "Makefile"


def _write_source(tmp_path: Path, filename: str, contents: str) -> Path:
    """Write a named source fixture and return its path.

    For example, `_write_source(tmp_path, "Makefile", "TY_VERSION ?= 0.0.89")`
    returns the path of a temporary Makefile containing that default.
    """
    source_path = tmp_path / filename
    source_path.write_text(contents, encoding="utf-8")
    return source_path


def _read_dev_group_requirements(pyproject_path: Path) -> list[str]:
    """Load validated string requirements from the development group.

    For `dev = ["ty==0.0.89"]`, this returns `["ty==0.0.89"]`.
    """
    project_metadata = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    try:
        dev_group = project_metadata["dependency-groups"]["dev"]
    except KeyError as error:
        message = f"{pyproject_path.name} is missing [dependency-groups].dev."
        raise AssertionError(message) from error
    if not isinstance(dev_group, list):
        message = f"{pyproject_path.name} [dependency-groups].dev must be a list."
        raise TypeError(message)

    requirements: list[str] = []
    for requirement in dev_group:
        if not isinstance(requirement, str):
            message = f"{pyproject_path.name} has a non-string dev requirement."
            raise TypeError(message)
        requirements.append(requirement)
    return requirements


def _read_dev_group_version(package_name: str, pyproject_path: Path) -> str:
    """Read one exact package version from the development dependency group.

    For a `ty==0.0.89` requirement, `_read_dev_group_version("ty", path)`
    returns `"0.0.89"`.
    """
    for requirement in _read_dev_group_requirements(pyproject_path):
        name, separator, version = requirement.partition("==")
        if name.strip().lower() != package_name:
            continue
        if not separator or not version:
            message = (
                f"{pyproject_path.name} dev group requires an exact "
                f"{package_name} version pin."
            )
            raise AssertionError(message)
        return version

    message = (
        f"{pyproject_path.name} [dependency-groups].dev is missing an exact "
        f"{package_name}==version pin."
    )
    raise AssertionError(message)


def _read_makefile_default(variable: str, makefile_path: Path) -> str:
    """Read a variable's default version from a Makefile."""
    pattern = rf"^{re.escape(variable)} \?= (\S+)$"
    match = re.search(pattern, makefile_path.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        message = (
            f"{makefile_path.name} is missing the default {variable} ?= <version>."
        )
        raise AssertionError(message)
    return match.group(1)


def _assert_tool_pin_matches(
    package_name: str,
    make_variable: str,
    pyproject_path: Path,
    makefile_path: Path,
) -> None:
    """Require the dev-group version and isolated Make default to agree.

    For `ty==0.0.89` and `TY_VERSION ?= 0.0.89`, this returns without raising.
    """
    dev_version = _read_dev_group_version(package_name, pyproject_path)
    makefile_version = _read_makefile_default(make_variable, makefile_path)
    if dev_version != makefile_version:
        message = (
            f"{pyproject_path.name} pins {package_name}=={dev_version}, but "
            f"{makefile_path.name} sets {make_variable} ?= {makefile_version}."
        )
        raise AssertionError(message)


@pytest.mark.parametrize(
    ("package_name", "make_variable"),
    [("ty", "TY_VERSION"), ("ruff", "RUFF_VERSION")],
    ids=("ty", "ruff"),
)
def test_isolated_tool_default_matches_dev_group_pin(
    package_name: str, make_variable: str
) -> None:
    """Require each isolated tool's Make default to equal its dev-group pin."""
    _assert_tool_pin_matches(package_name, make_variable, PYPROJECT, MAKEFILE)


@pytest.mark.parametrize(
    "invalid_pin",
    [
        pytest.param(
            (None, AssertionError, r"missing \[dependency-groups\]\.dev"),
            id="missing-group",
        ),
        pytest.param(
            ("dev = 'ty==0.0.89'", TypeError, "must be a list"),
            id="non-list-group",
        ),
        pytest.param(
            ("dev = [7]", TypeError, "non-string"),
            id="non-string-requirement",
        ),
        pytest.param(
            (
                "dev = ['ty>=0.0.89']",
                AssertionError,
                "missing an exact ty==version pin",
            ),
            id="non-exact-pin",
        ),
        pytest.param(
            (
                "dev = ['ruff==0.16.8']",
                AssertionError,
                "missing an exact ty==version pin",
            ),
            id="missing-package-pin",
        ),
    ],
)
def test_dev_group_pin_errors_are_explicit(
    tmp_path: Path, invalid_pin: tuple[str | None, type[Exception], str]
) -> None:
    """Malformed or missing dev pins fail with a direct, useful error."""
    dev_group, error_type, message = invalid_pin
    contents = (
        "[project]\nname = 'pin-contract'\n"
        if dev_group is None
        else f"[dependency-groups]\n{dev_group}\n"
    )
    pyproject = _write_source(tmp_path, "pyproject.toml", contents)

    with pytest.raises(error_type, match=message):
        _read_dev_group_version("ty", pyproject)


def test_makefile_pin_default_must_exist(tmp_path: Path) -> None:
    """A missing isolated-tool default fails rather than skipping the contract."""
    makefile = _write_source(tmp_path, "Makefile", "# no tool defaults\n")

    with pytest.raises(AssertionError, match="missing the default TY_VERSION"):
        _read_makefile_default("TY_VERSION", makefile)


def test_pin_mismatch_names_both_files_and_versions(tmp_path: Path) -> None:
    """A mismatched pin identifies each source and version in its failure."""
    pyproject = _write_source(
        tmp_path,
        "pyproject.toml",
        "[dependency-groups]\ndev = ['ty==0.0.89']\n",
    )
    makefile = _write_source(tmp_path, "Makefile", "TY_VERSION ?= 0.0.88\n")

    with pytest.raises(
        AssertionError,
        match=r"pyproject.toml pins ty==0\.0\.89, but Makefile sets TY_VERSION \?= 0\.0\.88",
    ):
        _assert_tool_pin_matches("ty", "TY_VERSION", pyproject, makefile)
