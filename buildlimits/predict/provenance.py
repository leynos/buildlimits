"""The commit a benchmark report names as its provenance.

The commit is read from the repository holding this package rather than from
the working directory a report happens to be produced in. A copy installed
inside another project's virtual environment sits in that project's
repository, so Git is asked both whether it holds the module at its canonical
path and whether it tracks the file before its commit is accepted.
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
    inside the repository is verified, and Git is asked whether it tracks that
    path, before the commit is accepted.

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
    return commit.strip() if _owns(executable, root.strip(), source) else "unknown"


def _owns(executable: str, root: str, source: Path) -> bool:
    """Return whether the repository at ``root`` holds ``source`` as its module.

    The path comparison rules out a copy that sits anywhere else in a host
    project, and Git is then asked whether it tracks the file: a copy dropped
    at the canonical path inside an unrelated repository would otherwise
    borrow that repository's commit.

    Returns
    -------
    bool
        Whether the module is tracked at its canonical path in that repository.
    """
    canonical = Path(root) / "buildlimits" / "predict" / source.name
    if not root or canonical.resolve() != source:
        return False
    return _tracked(executable, root, source)


def _tracked(executable: str, root: str, source: Path) -> bool:
    """Return whether ``git ls-files`` lists ``source`` in that repository.

    Returns
    -------
    bool
        Whether Git reports the file as tracked.
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
                "ls-files",
                "--error-unmatch",
                "--",
                str(relative),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return result.returncode == 0
