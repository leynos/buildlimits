"""Verify workspace-driver identity before classifying its rustc query."""

import typing as typ
from pathlib import Path

from buildlimits.rustc_query_grammar import is_query
from buildlimits.toolchain_env import locate_executable

if typ.TYPE_CHECKING:
    import collections.abc as cabc

WORKSPACE_DRIVERS = frozenset({"clippy-driver", "dylint-driver"})
WORKSPACE_ARGV_MINIMUM = 3


def is_workspace_query(
    argv: tuple[str, ...], env: cabc.Mapping[str, str], cwd: str
) -> bool:
    """Recognize a query behind one verified Clippy or Dylint driver.

    Returns
    -------
    bool
        Whether the single workspace wrapper and inner query are allow-listed.
    """
    parts = _workspace_query_parts(argv)
    if parts is None:
        return False
    driver, driver_name, arguments = parts
    if not _is_recognized_driver(driver, driver_name, env, cwd):
        return False
    if driver_name == "dylint-driver" and _has_dylint_flags(env):
        return False
    return is_query(arguments, has_driver=True)


def _workspace_query_parts(
    argv: tuple[str, ...],
) -> tuple[str, str, tuple[str, ...]] | None:
    """Return driver identity and query arguments for one supported wrapper.

    Returns
    -------
    tuple[str, str, tuple[str, ...]] | None
        The driver path, basename and query arguments, or ``None`` if invalid.
    """
    if len(argv) < WORKSPACE_ARGV_MINIMUM:
        return None
    driver, compiler = argv[:2]
    driver_name = Path(driver).name
    if driver_name not in WORKSPACE_DRIVERS or Path(compiler).name != "rustc":
        return None
    return driver, driver_name, argv[2:]


def _is_recognized_driver(
    driver: str, driver_name: str, env: cabc.Mapping[str, str], cwd: str
) -> bool:
    """Verify configured driver identity, or use the closed name allow-list.

    Returns
    -------
    bool
        Whether the path resolves to the configured workspace driver.
    """
    if "RUSTC_WORKSPACE_WRAPPER" not in env:
        return driver_name in WORKSPACE_DRIVERS

    configured = env["RUSTC_WORKSPACE_WRAPPER"]
    if not isinstance(configured, str) or not configured:
        return False
    try:
        actual_path = locate_executable(driver, env, cwd)
        configured_path = locate_executable(configured, env, cwd)
        if actual_path is None or configured_path is None:
            return False
        return Path(actual_path).samefile(configured_path)
    except OSError, TypeError, ValueError:
        return False


def _has_dylint_flags(env: cabc.Mapping[str, str]) -> bool:
    """Detect Dylint flags that can add unclassified compiler arguments.

    Returns
    -------
    bool
        Whether non-whitespace flags are present.
    """
    flags = env.get("DYLINT_RUSTFLAGS", "")
    return not isinstance(flags, str) or bool(flags.strip())
