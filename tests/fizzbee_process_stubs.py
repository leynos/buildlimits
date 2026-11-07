"""The launcher stub and specification the FizzBee process tests share.

Three test modules drive :class:`modelcheck.adapters.fizzbee_process.FizzbeeChecker`
with a shell script in place of the real binary: the adapter's own tests, the
process-group kill tests and the evidence tests. They need the same two things —
a specification to run and an executable script to run it — so both live here
rather than in one of the ``test_*.py`` modules, which the repository keeps for
tests alone.

The script is written into the test's scratch directory and made executable.
A stub that merely exited would let an adapter that never ran anything pass, so
each caller writes a script that records what it was asked and reads the record
back.
"""

import typing as typ

from modelcheck import domain

if typ.TYPE_CHECKING:
    from pathlib import Path


def stub(directory: Path, script: str, name: str = "fizz") -> Path:
    """Write an executable launcher into a directory.

    Parameters
    ----------
    directory : pathlib.Path
        Where to write the script.
    script : str
        The script's text.
    name : str
        The file name to give it.

    Returns
    -------
    pathlib.Path
        The script, made executable.
    """
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def spec() -> domain.MaterializedSpec:
    """Return a specification to run.

    Returns
    -------
    domain.MaterializedSpec
        A minimal specification; the stub answers regardless of its content.
    """
    return domain.MaterializedSpec("head", "action Init:\n  n = 0\n", ())
