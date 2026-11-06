"""The fixture paths, environment and process runner the end-to-end tests use.

The end-to-end module runs ``python -m modelcheck`` as a maintainer would, with
a stub launcher in place of the checker binary. Everything that makes such a
run reproducible — where the fixture tree is, which environment the stub sees,
and how its record is read back — lives here so the tests themselves read as
statements about behaviour rather than as process plumbing.

This module is test support and is linted as production code, following the
convention in ``docs/repository-layout.md``: ``test_*.py`` modules hold tests,
anything else here holds helpers.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
"""The checkout root, which is where ``uv run`` finds the project."""

FIXTURES = Path(__file__).with_name("fixtures") / "modelcheck"
"""The fixture directory holding the model, its configurations and the stub."""

MODEL = FIXTURES / "model"
"""The fixture model directory the command line is pointed at."""

FIZZ = FIXTURES / "fizz"
"""The stub launcher that stands in for the checker binary."""

TIMEOUT_SECONDS = 120
"""Enough for ``uv`` to resolve the project and the run to finish twice over.

The matrix itself is three stub invocations of a few milliseconds. The bound is
generous because the first ``uv run`` in a session may have to check the lock
file, and a flaky bound would be blamed on the code under test.
"""


def ambient() -> dict[str, str]:
    """Return the caller's environment with every ``MODELCHECK_*`` removed.

    Stripping the whole prefix rather than only the names a test passes is what
    stops an ambient ``MODELCHECK_FIZZ`` or ``MODELCHECK_BOUNDS`` from changing
    what a test measures. Both helpers start here, so a variable added to one
    run's environment cannot leak into the other's by an oversight.

    Returns
    -------
    dict[str, str]
        The caller's environment, without any ``MODELCHECK_*`` setting.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MODELCHECK_")
    }


def environment(log: Path, **overrides: str) -> dict[str, str]:
    """Return the environment one command-line run gets.

    Parameters
    ----------
    log : pathlib.Path
        Where the stub is to record each invocation.
    **overrides : str
        Variables to set or replace for this run.

    Returns
    -------
    dict[str, str]
        The ambient environment, plus the stub's log and the caller's
        overrides.
    """
    variables = ambient()
    variables["MODELCHECK_E2E_LOG"] = str(log)
    variables.update(overrides)
    return variables


def run(
    *arguments: str, log: Path, **overrides: str
) -> subprocess.CompletedProcess[str]:
    """Run the command line as a maintainer would.

    Parameters
    ----------
    *arguments : str
        The arguments after ``python -m modelcheck``.
    log : pathlib.Path
        Where the stub is to record each invocation.
    **overrides : str
        Variables to set or replace for this run.

    Returns
    -------
    subprocess.CompletedProcess
        The finished run, with its output decoded as text.
    """
    return subprocess.run(
        ["uv", "run", "python", "-m", "modelcheck", *arguments],
        cwd=ROOT,
        env=environment(log, **overrides),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def check(*extra: str, log: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    """Run ``modelcheck check`` over the fixture model.

    Parameters
    ----------
    *extra : str
        Further arguments to the ``check`` command.
    log : pathlib.Path
        Where the stub is to record each invocation.
    **overrides : str
        Variables to set or replace for this run.

    Returns
    -------
    subprocess.CompletedProcess
        The finished run.
    """
    return run("check", str(MODEL), "--fizz", str(FIZZ), *extra, log=log, **overrides)


def invocations(log: Path) -> list[tuple[str, str]]:
    """Return what the stub was asked to do, one pair per run.

    Parameters
    ----------
    log : pathlib.Path
        The file the stub appended to.

    Returns
    -------
    list[tuple[str, str]]
        Each run's working directory and the arguments it was given.
    """
    lines = log.read_text(encoding="utf-8").splitlines()
    return list(zip(lines[0::2], lines[1::2], strict=True))


def paths(root: Path) -> list[Path]:
    """Return every path under a directory.

    Parameters
    ----------
    root : pathlib.Path
        The directory to list.

    Returns
    -------
    list[Path]
        The paths beneath it, relative to it and sorted, so two snapshots
        compare as sets of names rather than as directory listings.
    """
    return sorted(path.relative_to(root) for path in root.rglob("*"))


def tree() -> list[Path]:
    """Return every path under the fixture directory.

    Returns
    -------
    list[Path]
        The fixture tree's paths, relative to it and sorted.
    """
    return paths(FIXTURES)
