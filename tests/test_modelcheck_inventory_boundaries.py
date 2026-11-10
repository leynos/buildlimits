"""The inventory query is a read: it writes nothing beside the sources it reads.

:mod:`tests.test_modelcheck_inventory` says what the inventory enumerates.
What it cannot say is the boundary the adapter relies on to be usable at all:
a query run over a checkout must not create, rewrite or remove a file in either
package.

Loading and querying are separate steps. :meth:`BuildlimitsInventory.load`
imports the production package inside :func:`_no_bytecode`, so even a cold
cache is read rather than compiled, and the inventory it returns answers
:meth:`BuildlimitsInventory.items` and
:meth:`BuildlimitsInventory.resolves` from a set it already holds. Two
observations pin that down. The first loads the inventory cold and compares the
production sources with themselves. The second runs the real drift command,
``python -m modelcheck drift``, over a copied checkout and compares both
packages. Neither observation sets ``PYTHONDONTWRITEBYTECODE`` or a cache
prefix, so a run that wanted to write would be free to.

The copies are what make the comparison whole. A snapshot of the checkout
would include caches written by the test session that collected this file, and
a write into it would dirty the working tree; a copy starts from the sources
alone, so every file the run creates is one the run is answerable for.

The subprocesses are what make the run cold. A module imported by this test
file would already be in ``sys.modules``, so an in-process query would prove
nothing about the imports it does or does not perform. The drift command is
warmed through ``--help`` first: the interpreter compiles the tool it was
asked to run before any command exists, and that startup write is the
interpreter's, not the check's. The production package is left cold in both
observations, which is the cache a drift check must not write.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from directory_state_test_helpers import FileState, snapshot_tree

_REPOSITORY = Path(__file__).resolve().parents[1]
"""The checkout whose packages the query reads."""

_READ_TREES = ("buildlimits", "modelcheck")
"""The packages the run reads, and so must not change."""

_MODEL = Path("models/admission/coupling")
"""The model the copied drift command checks."""

_LOAD = """\
import os

import modelcheck
from modelcheck.adapters.inventory import BuildlimitsInventory

copy = os.path.realpath(os.environ["MODELCHECK_COPY"])
assert os.path.realpath(modelcheck.__file__).startswith(copy + os.sep), (
    modelcheck.__file__
)
inventory = BuildlimitsInventory.load()
assert inventory.items(), "the inventory is empty"
print(len(inventory.items()))
"""
"""Load the inventory in a cold interpreter over the copied packages."""

_HELP = (sys.executable, "-m", "modelcheck", "--help")
"""Build the command line, so the tool's own bytecode is already cached."""

_DRIFT = (sys.executable, "-m", "modelcheck", "drift", str(_MODEL))
"""The real command, run from a directory holding nothing but the copies."""


def _copied_checkout(root: Path) -> None:
    """Copy the packages and the model into a scratch directory.

    Parameters
    ----------
    root : pathlib.Path
        The directory the copies are written under. Existing bytecode caches
        are left behind, so the copies start from the sources alone and every
        cache file the run creates is one the run is answerable for.
    """
    for name in (*_READ_TREES, "models"):
        shutil.copytree(
            _REPOSITORY / name,
            root / name,
            ignore=shutil.ignore_patterns("__pycache__"),
        )


def _query_environment(root: Path) -> dict[str, str]:
    """Return the environment the subprocesses run with.

    Parameters
    ----------
    root : pathlib.Path
        The directory holding the copied packages.

    Returns
    -------
    dict[str, str]
        The caller's environment with the two bytecode settings removed, so the
        run uses the interpreter's default cache location whatever the caller
        had configured, and with the copy first on the import path.
    """
    environment = dict(os.environ)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment["MODELCHECK_COPY"] = str(root)
    environment["PYTHONPATH"] = str(root)
    return environment


def _run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[str]:
    """Run one command in a cold interpreter over the copied packages.

    Parameters
    ----------
    argv : tuple[str, ...]
        The program and its arguments.
    root : pathlib.Path
        The directory holding the copied packages, which is also the working
        directory so the copy is the first entry on the import path.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The completed run, with its output captured.
    """
    return subprocess.run(
        argv,
        cwd=root,
        env=_query_environment(root),
        capture_output=True,
        text=True,
        check=False,
    )


def _snapshot(root: Path, name: str) -> dict[str, FileState]:
    """Return the files a copied tree holds, bytecode caches included.

    Parameters
    ----------
    root : pathlib.Path
        The directory the tree was copied under.
    name : str
        The tree's name.

    Returns
    -------
    dict[str, FileState]
        The snapshot to compare across a run.
    """
    return snapshot_tree(root / name, relative_to=root)


def test_a_cold_inventory_load_writes_nothing_beside_the_sources(
    tmp_path: Path,
) -> None:
    """Loading the inventory cold leaves the production package untouched.

    The copies hold no bytecode, and the run is made with the interpreter's
    default cache settings, so a load that compiled what it read would leave a
    ``__pycache__`` entry. There is none: the import is made with bytecode
    writing switched off.
    """
    root = tmp_path / "checkout"
    _copied_checkout(root)
    before = _snapshot(root, "buildlimits")

    completed = _run((sys.executable, "-c", _LOAD), root)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().isdigit(), "the load printed its inventory size"
    assert _snapshot(root, "buildlimits") == before, (
        "the load created, rewrote or removed a file in the production package"
    )


def test_a_cold_drift_command_creates_no_file(tmp_path: Path) -> None:
    """The real drift command leaves both copied packages exactly as it found them.

    The tool's own startup is warmed first, because the interpreter compiles
    the code it is about to run before the command exists. The production
    package stays cold, and the drift run that follows must add nothing: no
    new ``__pycache__`` entry, no rewritten source, no removed file.
    """
    root = tmp_path / "checkout"
    _copied_checkout(root)
    warmed = _run(_HELP, root)
    assert warmed.returncode == 0, warmed.stderr
    assert not list((root / "buildlimits").rglob("__pycache__")), (
        "the warm-up compiled the production package, so the drift run is not cold"
    )

    before = {name: _snapshot(root, name) for name in _READ_TREES}

    completed = _run(_DRIFT, root)

    assert completed.returncode == 0, completed.stderr
    assert "the model accounts for every production path" in completed.stdout, (
        completed.stdout
    )
    after = {name: _snapshot(root, name) for name in _READ_TREES}
    assert after == before, "the drift command created, rewrote or removed a file"
