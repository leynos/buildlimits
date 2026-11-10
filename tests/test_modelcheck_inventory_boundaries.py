"""The inventory is a read: a query writes nothing beside the sources it reads.

:mod:`tests.test_modelcheck_inventory` says what the inventory enumerates.
What it cannot say is the boundary the adapter relies on to be usable at all,
because the adapter reaches the production surface by importing it: a query
run over a checkout must not create, rewrite or remove a file in either package.

An import may write bytecode, and the interpreter decides where. This test
therefore runs the query in a cold subprocess with ``PYTHONPYCACHEPREFIX`` set
to a directory of its own, and asserts both halves of the result: the cache
directory receives the compiled modules, which proves the imports really
happened, and the two source trees are byte-identical afterwards. Redirecting
the cache is the only place the write goes when a caller asks for it, so the
query itself holds no write of its own.

The subprocess is what makes the query cold. A module imported by this test
file would already be in ``sys.modules``, so an in-process query would prove
nothing about the import it performs.
"""

import os
import subprocess
import sys
from pathlib import Path

from directory_state_test_helpers import snapshot_tree

_REPOSITORY = Path(__file__).resolve().parents[1]
"""The checkout the query reads."""

_READ_TREES = ("buildlimits", "modelcheck")
"""The packages an inventory query reads, and so must not change."""

_QUERY = """\
from modelcheck.adapters.inventory import BuildlimitsInventory

items = BuildlimitsInventory().items()
assert items, "the inventory is empty"
print(len(items))
"""
"""The cold query, as the interpreter runs it."""


def _query_environment(cache: Path) -> dict[str, str]:
    """Return the environment the cold query runs with.

    Parameters
    ----------
    cache : pathlib.Path
        Where the interpreter is told to write its bytecode.

    Returns
    -------
    dict[str, str]
        The caller's environment with the two bytecode settings replaced, so
        the run writes its cache where the test can see it whatever the caller
        had configured.
    """
    environment = dict(os.environ)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment["PYTHONPYCACHEPREFIX"] = str(cache)
    return environment


def test_a_cold_query_writes_nothing_beside_the_sources(tmp_path: Path) -> None:
    """The inventory reads the production package and leaves it as it was.

    The query has to import the package to enumerate it, so the interpreter
    will compile the modules it reads; the assertion is that the compiled
    output goes to the cache the caller named and that no file in either source
    tree is created, rewritten or removed. The cache half is asserted first, so
    a query that failed to import anything fails here rather than passing the
    comparison by doing nothing.
    """
    cache = tmp_path / "pycache"
    before = {
        name: snapshot_tree(_REPOSITORY / name, relative_to=_REPOSITORY)
        for name in _READ_TREES
    }

    completed = subprocess.run(
        [sys.executable, "-c", _QUERY],
        cwd=_REPOSITORY,
        env=_query_environment(cache),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().isdigit(), "the query printed its inventory size"
    assert list(cache.rglob("*.pyc")), "the imports were compiled into the cache"
    after = {
        name: snapshot_tree(_REPOSITORY / name, relative_to=_REPOSITORY)
        for name in _READ_TREES
    }
    assert after == before, "the query created, rewrote or removed a file"
