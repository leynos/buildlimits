"""Keep isolated quality tools aligned with their development dependencies."""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
MAKEFILE = ROOT / "Makefile"


def _read_dev_group_requirements() -> list[str]:
    """Load validated string requirements from the development group.

    For `dev = ["ty==0.0.89"]`, this returns `["ty==0.0.89"]`.
    """
    project_metadata = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    try:
        dev_group = project_metadata["dependency-groups"]["dev"]
    except KeyError as error:
        raise AssertionError(
            f"{PYPROJECT.name} is missing [dependency-groups].dev."
        ) from error
    if not isinstance(dev_group, list):
        raise TypeError(f"{PYPROJECT.name} [dependency-groups].dev must be a list.")

    requirements: list[str] = []
    for requirement in dev_group:
        if not isinstance(requirement, str):
            raise TypeError(f"{PYPROJECT.name} has a non-string dev requirement.")
        requirements.append(requirement)
    return requirements


def _read_dev_group_version(package_name: str) -> str:
    """Read one exact package version from the project's dev dependency group.

    For a `ty==0.0.89` requirement, `_read_dev_group_version("ty")` returns
    `"0.0.89"`.
    """
    for requirement in _read_dev_group_requirements():
        name, separator, version = requirement.partition("==")
        if name.strip().lower() != package_name:
            continue
        if not separator or not version:
            raise AssertionError(
                f"{PYPROJECT.name} dev group requires an exact {package_name} version pin."
            )
        return version

    raise AssertionError(
        f"{PYPROJECT.name} [dependency-groups].dev is missing an exact "
        f"{package_name}==version pin."
    )


def _read_makefile_default(variable: str) -> str:
    """Read a variable's default version from the Makefile."""
    pattern = rf"^{re.escape(variable)} \?= (\S+)$"
    match = re.search(pattern, MAKEFILE.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise AssertionError(
            f"{MAKEFILE.name} is missing the default {variable} ?= <version>."
        )
    return match.group(1)


@pytest.mark.parametrize(
    ("package_name", "make_variable"),
    [("ty", "TY_VERSION"), ("ruff", "RUFF_VERSION")],
    ids=("ty", "ruff"),
)
def test_isolated_tool_default_matches_dev_group_pin(
    package_name: str, make_variable: str
) -> None:
    """Require each isolated tool's Make default to equal its dev-group pin."""
    dev_version = _read_dev_group_version(package_name)
    makefile_version = _read_makefile_default(make_variable)
    if dev_version != makefile_version:
        raise AssertionError(
            f"{PYPROJECT.name} pins {package_name}=={dev_version}, but "
            f"{MAKEFILE.name} sets {make_variable} ?= {makefile_version}."
        )
