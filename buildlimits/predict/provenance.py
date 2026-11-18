"""The commit a benchmark report names as its provenance.

The commit is read from the repository holding this package rather than from
the working directory a report happens to be produced in. A copy installed
inside another project's virtual environment sits in that project's
repository, so Git is asked both whether it holds the module at its canonical
path and whether the reported commit's tree holds the file before that commit
is accepted.
"""

import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed git command, never a shell.
from pathlib import Path


def git_revision() -> str:
    """Return the commit that holds this benchmark, or an explicit marker.

    The commit is read from the repository holding this module rather than
    from the working directory, so a report produced inside an unrelated
    checkout still names the code that produced it. A copy installed inside
    another project's virtual environment sits in that project's repository,
    whose commit is not this benchmark's provenance, so the module's own path
    inside the repository is verified, and the commit's tree is asked whether
    it holds that path, before the commit is accepted.

    Returns
    -------
    str
        The full commit hash, or ``unknown``.
    """
    executable = shutil.which("git")
    if executable is None:
        return "unknown"
    source = Path(__file__).resolve()
    try:
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed git argument vector.
            [
                executable,
                "-C",
                str(source.parent),
                "rev-parse",
                "HEAD",
                "--show-toplevel",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    commit, _, root = result.stdout.partition("\n")
    return (
        commit.strip()
        if _owns(executable, commit.strip(), root.strip(), source)
        else "unknown"
    )


def _owns(executable: str, commit: str, root: str, source: Path) -> bool:
    """Return whether ``commit`` holds ``source`` as the repository's module.

    The path comparison rules out a copy that sits anywhere else in a host
    project, and the commit's tree is then asked for the file: a copy dropped
    at the canonical path inside an unrelated repository would otherwise
    borrow that repository's commit.

    Returns
    -------
    bool
        Whether the module sits at its canonical path in that commit.
    """
    canonical = Path(root) / "buildlimits" / "predict" / source.name
    if not root or canonical.resolve() != source:
        return False
    return _tracked(executable, commit, root, source)


def _tracked(executable: str, commit: str, root: str, source: Path) -> bool:
    """Return whether the named commit's tree holds ``source``.

    The index is not enough. A file can be staged at the canonical path before
    it is committed, and ``git ls-files`` would list it while the commit the
    report names has no such entry, so the report would claim provenance for
    code the commit does not contain. Asking ``git cat-file`` for
    ``<commit>:<path>`` reads the tree the report actually names.

    Returns
    -------
    bool
        Whether the commit holds the file at that path.
    """
    try:
        relative = source.relative_to(Path(root).resolve())
    except ValueError:
        return False
    try:
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed git argument vector.
            [
                executable,
                "-C",
                root,
                "cat-file",
                "-e",
                f"{commit}:{relative}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return result.returncode == 0
