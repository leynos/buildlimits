"""The inventory query is a read: it writes nothing beside the sources it reads.

:mod:`tests.test_modelcheck_inventory` says what the inventory enumerates.
What it cannot say is the boundary the adapter relies on to be usable at all:
a query run over a checkout must not create, rewrite or remove a file in either
package.

Loading and querying are separate steps. :meth:`BuildlimitsInventory.load`
imports the production package, and an import may write bytecode; the
inventory it returns answers :meth:`BuildlimitsInventory.items` and
:meth:`BuildlimitsInventory.resolves` from a set it already holds. This test
therefore copies both packages into a scratch directory, warms the bytecode
cache there with one cold subprocess, snapshots the copies, and runs the query
in a second cold subprocess under the interpreter's default bytecode settings.
The copies must be byte-identical afterwards, ``__pycache__`` included, so the
query itself holds no write of its own.

The copies are what make the comparison whole. A snapshot of the checkout
would include caches written by the test session that collected this file, and
a write into it would dirty the working tree; a copy starts from the sources
alone, so every file the run creates is one the run is answerable for. The
subprocesses are what make the query cold: a module imported by this test file
would already be in ``sys.modules``, so an in-process query would prove nothing
about the imports it does or does not perform.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from directory_state_test_helpers import snapshot_tree

_REPOSITORY = Path(__file__).resolve().parents[1]
"""The checkout whose packages the query reads."""

_READ_TREES = ("buildlimits", "modelcheck")
"""The packages an inventory query reads, and so must not change."""

_LOAD = """\
import os

import modelcheck
from modelcheck.adapters.inventory import BuildlimitsInventory

copy = os.path.realpath(os.environ["MODELCHECK_COPY"])
assert os.path.realpath(modelcheck.__file__).startswith(copy + os.sep), (
    modelcheck.__file__
)
BuildlimitsInventory.load()
print("loaded")
"""
"""The loading step, run once so the query starts from a warm bytecode cache."""

_QUERY = """\
import os

import modelcheck
from modelcheck.adapters.inventory import BuildlimitsInventory

copy = os.path.realpath(os.environ["MODELCHECK_COPY"])
assert os.path.realpath(modelcheck.__file__).startswith(copy + os.sep), (
    modelcheck.__file__
)
inventory = BuildlimitsInventory.load()
items = inventory.items()
assert items, "the inventory is empty"
assert inventory.resolves("buildlimits.effects.Launch"), "a known path resolves"
print(len(items))
"""
"""The cold query, as the interpreter runs it."""


def _copied_packages(root: Path) -> None:
    """Copy the two packages into a scratch directory.

    Parameters
    ----------
    root : pathlib.Path
        The directory the copies are written under. Existing bytecode caches
        are left behind, so the copies start from the sources alone and every
        cache file the run creates is one the run is answerable for.
    """
    for name in _READ_TREES:
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


def _run(script: str, root: Path) -> subprocess.CompletedProcess[str]:
    """Run one script in a cold interpreter over the copied packages.

    Parameters
    ----------
    script : str
        The program to run.
    root : pathlib.Path
        The directory holding the copied packages, which is also the working
        directory so the copy is the first entry on the import path.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The completed run, with its output captured.
    """
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=_query_environment(root),
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_cold_query_writes_nothing_beside_the_sources(tmp_path: Path) -> None:
    """The inventory query leaves both copied packages exactly as it found them.

    The loading step runs first, outside the observation window, because it is
    the one step allowed to import the production package and so the one step
    that may write bytecode. The query that follows must add nothing: no new
    ``__pycache__`` entry, no rewritten source, no removed file.
    """
    root = tmp_path / "checkout"
    _copied_packages(root)

    loaded = _run(_LOAD, root)
    assert loaded.returncode == 0, loaded.stderr

    before = {
        name: snapshot_tree(root / name, relative_to=root) for name in _READ_TREES
    }

    completed = _run(_QUERY, root)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().isdigit(), "the query printed its inventory size"
    after = {name: snapshot_tree(root / name, relative_to=root) for name in _READ_TREES}
    assert after == before, "the query created, rewrote or removed a file"
