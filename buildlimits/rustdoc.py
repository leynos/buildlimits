"""Resolve the real rustdoc that the caller's toolchain selection names.

The rustdoc adapter replaces ``RUSTDOC``, so it cannot find the real rustdoc
on ``PATH``: it asks the provisioned rustup instead, which honours
``RUSTUP_TOOLCHAIN`` and directory overrides. Running rustup is a port,
:func:`run_rustup` by default, and every way the lookup can fail is reported
as :class:`RustdocError`.
"""

import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argument vector, never a shell.
import sys
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class RustdocError(Exception):
    """The real rustdoc cannot be found."""


def run_rustup(
    argv: cabc.Sequence[str], cwd: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run rustup with a fixed argument vector, capturing its output.

    Parameters
    ----------
    argv : Sequence[str]
        The rustup command; never interpreted by a shell.
    cwd : str
        The directory whose toolchain overrides apply.
    env : dict[str, str]
        The environment to run it in.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The finished command, whatever its exit status.
    """
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argument vector.
        list(argv), cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def resolve_rustdoc(
    env: cabc.Mapping[str, str],
    cwd: str,
    run: cabc.Callable[
        [cabc.Sequence[str], str, dict[str, str]], subprocess.CompletedProcess[str]
    ] = run_rustup,
) -> str:
    """Find the rustdoc that the caller's toolchain selection names.

    Uses the provisioned rustup, which respects ``RUSTUP_TOOLCHAIN`` and
    directory overrides from the working directory.

    Parameters
    ----------
    env : Mapping[str, str]
        The caller's environment.
    cwd : str
        The caller's working directory.
    run : Callable
        Runs rustup; :func:`run_rustup` by default.

    Returns
    -------
    str
        The absolute path of the real rustdoc.

    Raises
    ------
    RustdocError
        If rustup cannot be run or cannot resolve rustdoc, or resolves this
        adapter.
    """
    try:
        result = run([_find_rustup(env), "which", "rustdoc"], cwd, dict(env))
    except (OSError, subprocess.TimeoutExpired) as error:
        msg = f"cannot run rustup to resolve rustdoc: {error}"
        raise RustdocError(msg) from error
    resolved = result.stdout.strip()
    if result.returncode != 0 or not resolved:
        msg = f"rustup cannot resolve rustdoc: {result.stderr.strip()}"
        raise RustdocError(msg)
    if Path(resolved).resolve() == Path(sys.argv[0]).resolve():
        msg = "rustup resolved rustdoc to this adapter"
        raise RustdocError(msg)
    return resolved


def _find_rustup(env: cabc.Mapping[str, str]) -> str:
    """Locate the provisioned rustup.

    Returns
    -------
    str
        ``BUILD_LIMITS_RUSTUP``, or the ``rustup`` on the caller's ``PATH``.

    Raises
    ------
    RustdocError
        If neither exists.
    """
    rustup = env.get("BUILD_LIMITS_RUSTUP") or shutil.which(
        "rustup", path=env.get("PATH")
    )
    if not rustup:
        msg = "cannot find rustup to resolve rustdoc; set BUILD_LIMITS_RUSTUP"
        raise RustdocError(msg)
    return rustup
