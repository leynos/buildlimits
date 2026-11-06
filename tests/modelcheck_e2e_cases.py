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

import json
import os
import subprocess
import typing as typ
from pathlib import Path

import pytest

if typ.TYPE_CHECKING:
    from modelcheck_artefacts import Record

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


def persisted(
    tmp_path: Path, answer: str, **overrides: str
) -> tuple[int, list[Record]]:
    """Run the matrix with one stub answer and read the record it persisted.

    Parameters
    ----------
    tmp_path : pathlib.Path
        The test's scratch directory; the record and the stub's log both go
        here rather than beside the fixture.
    answer : str
        The stub's ``MODELCHECK_E2E_ANSWER``, which selects the checker output
        the run replays.
    **overrides : str
        Further environment variables for this run.

    Returns
    -------
    tuple[int, list[Record]]
        The command's exit status and the records it wrote.
    """
    records = tmp_path / "matrix.json"
    result = check(
        "--json",
        str(records),
        log=tmp_path / "seen.log",
        MODELCHECK_E2E_ANSWER=answer,
        **overrides,
    )
    written = typ.cast("list[Record]", json.loads(records.read_text(encoding="utf-8")))
    return result.returncode, written


def assert_holds(text: str, expected: dict[str, str]) -> None:
    """Assert that ``text`` contains each phrase, naming the claim that failed.

    Parameters
    ----------
    text : str
        The diagnostic being checked.
    expected : dict[str, str]
        Each claim a maintainer should be able to make about the text, mapped
        to the phrase that establishes it. The claim is the failure message,
        because a bare phrase match does not say why the phrase matters.

    Notes
    -----
    Each phrase is a distinct behavioural claim about the record — the reason
    names the read, the transcript is kept, the runner path is replaced — so
    this is not a snapshot of a rendered document. A snapshot would pin the
    transcript's four thousand characters and the reader would still have to
    work out which part carries which claim.
    """
    missing = {
        claim: phrase for claim, phrase in expected.items() if phrase not in text
    }

    if missing:
        pytest.fail(f"the record omits {missing}")


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


def contents(root: Path) -> dict[Path, bytes]:
    """Return every file under a directory with the bytes it holds.

    Parameters
    ----------
    root : pathlib.Path
        The directory to read.

    Returns
    -------
    dict[pathlib.Path, bytes]
        Each file's path, relative to the root, mapped to its contents. A
        listing of paths cannot tell a file that was rewritten from one that
        was not, and a run that changed a model file in place would leave the
        same set of names behind. Reading the bytes is what makes the
        difference visible.

    Examples
    --------
    >>> import tempfile
    >>> from pathlib import Path
    >>> with tempfile.TemporaryDirectory() as scratch:
    ...     root = Path(scratch)
    ...     _ = (root / "a.txt").write_text("a", encoding="utf-8")
    ...     contents(root) == {Path("a.txt"): b"a"}
    True
    """
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def tree() -> list[Path]:
    """Return every path under the fixture directory.

    Returns
    -------
    list[Path]
        The fixture tree's paths, relative to it and sorted.
    """
    return paths(FIXTURES)
