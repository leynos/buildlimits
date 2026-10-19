"""Run a real toolchain compiler that duplicates its driver, through the adapter.

The scenario needs an installed toolchain whose ``rustc-dev`` component put a
second ``librustc_driver`` in ``lib/rustlib/<triple>/lib``. It only asks the
compiler for its version, which loads the compiler without compiling anything,
so it needs no compile slot. The test skips when no such toolchain is installed.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

DRIVER_PATTERN = "librustc_driver-*.so"
# Stands in for a process that is already inside an admitted scope, so the
# adapter replaces itself with the compiler instead of asking a supervisor.
ADAPTER_SCRIPT = textwrap.dedent(
    """
    import sys
    from buildlimits import adapter
    adapter.in_admitted_scope = lambda env: True
    sys.exit(adapter._main(sys.argv[1:], cacheable=False))
    """
)


def _rustup_home() -> Path:
    """Return the directory that holds installed toolchains."""
    explicit = os.environ.get("RUSTUP_HOME")
    return Path(explicit) if explicit else Path.home() / ".rustup"


def _duplicating_toolchains() -> list[tuple[Path, Path]]:
    """List installed toolchains that duplicate their driver in rustlib.

    Returns
    -------
    list[tuple[Path, Path]]
        Each ``rustc`` with the rustlib directory that holds the duplicate.
    """
    return [
        (root / "bin" / "rustc", duplicate.parent)
        for root in sorted((_rustup_home() / "toolchains").glob("*"))
        if (root / "bin" / "rustc").is_file()
        and any((root / "lib").glob(DRIVER_PATTERN))
        for duplicate in root.glob(f"lib/rustlib/*/lib/{DRIVER_PATTERN}")
    ]


def _broken_toolchain() -> tuple[Path, Path] | None:
    """Find a toolchain whose real compiler the rustlib path actually breaks.

    A duplicate driver is not enough: some ``rustc-dev`` installs also keep a
    ``libLLVM`` in rustlib, and those compilers still start.

    Returns
    -------
    tuple[Path, Path] | None
        The first such ``rustc`` and rustlib directory, or ``None``.
    """
    for rustc, rustlib in _duplicating_toolchains():
        if _version([str(rustc), "--version"], str(rustlib)).returncode != 0:
            return rustc, rustlib
    return None


def _version(command: list[str], loader_path: str) -> subprocess.CompletedProcess[str]:
    """Run a command with a controlled loader path.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The finished command, whatever its status.
    """
    env = {**os.environ, "LD_LIBRARY_PATH": loader_path}
    return subprocess.run(
        command, env=env, capture_output=True, text=True, check=False, timeout=60
    )


def test_the_adapter_runs_a_real_compiler_that_the_poisoned_path_breaks() -> None:
    """The environment that stops the bare compiler starts it through the adapter."""
    found = _broken_toolchain()
    if found is None:
        pytest.skip("no installed toolchain fails to start under its rustlib path")
    rustc, rustlib = found
    through = _version(
        [sys.executable, "-c", ADAPTER_SCRIPT, str(rustc), "--version"], str(rustlib)
    )
    assert through.returncode == 0, through.stderr
    assert through.stdout.startswith("rustc "), "the compiler printed its version"
