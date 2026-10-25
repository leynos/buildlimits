"""Fail-closed recognition of non-compiling rustc toolchain queries.

Version queries and the allow-listed no-input ``--print`` queries follow
``rustc`` at commit
`a293cc4af8b26701c42738381c0c6f9d2ba881e0`
(``compiler/rustc_driver_impl/src/lib.rs``). The Cargo target-information
grammar follows commit
`fe2f314aef06e688a9517da1ac0577bb1854d01f`
(``src/cargo/core/compiler/build_context/target_info.rs``). Workspace-wrapper
identity and Dylint flag handling follow Dylint commit
`9adfa398661273ca7dc99df9bf2c26ae6f61b1c5` (``driver/src/lib.rs``).

Unknown arguments, wrappers and identities remain admitted. The filesystem is
consulted only when Cargo's ``RUSTC_WORKSPACE_WRAPPER`` is present, to confirm
that the recognized driver token names that configured executable.

Examples
--------
>>> is_toolchain_query(["/toolchain/bin/rustc", "-vV"], {}, "/")
True
>>> is_toolchain_query(["rustc", "--print=cfg", "--emit=link"], {}, "/")
False
>>> is_toolchain_query(["other-wrapper", "rustc", "-vV"], {}, "/")
False
"""

import os
import typing as typ
from pathlib import Path

from buildlimits.groups import compiler_argv
from buildlimits.rustc_cargo_probe import (
    following_argument as _following_argument,
)
from buildlimits.rustc_cargo_probe import (
    is_cargo_target_probe as _is_cargo_target_probe,
)
from buildlimits.toolchain_env import locate_executable

if typ.TYPE_CHECKING:
    import collections.abc as cabc

WORKSPACE_DRIVERS = frozenset({"clippy-driver", "dylint-driver"})
VERSION_FLAGS = frozenset({"-V", "--version", "-vV"})
PRINT_KINDS = frozenset({
    "sysroot",
    "target-libdir",
    "cfg",
    "target-list",
    "target-cpus",
    "target-features",
})
WORKSPACE_ARGV_MINIMUM = 3


def is_toolchain_query(
    argv: cabc.Sequence[str | bytes],
    env: cabc.Mapping[str, str],
    cwd: str,
) -> bool:
    """Return whether ``argv`` is an allow-listed rustc query.

    Parameters
    ----------
    argv : Sequence[str | bytes]
        Adapter argv, including the requested compiler and any wrappers.
    env : Mapping[str, str]
        Effective caller environment. Wrapper identity checks use only this
        mapping and never consult the process environment implicitly.
    cwd : str
        Caller working directory used to resolve a configured wrapper.

    Returns
    -------
    bool
        ``True`` only for a recognized query that cannot compile a crate.

    Examples
    --------
    >>> is_toolchain_query(["sccache", "/toolchain/bin/rustc", "--version"], {}, "/")
    True
    >>> is_toolchain_query(["rustc", "-vV", "lib.rs"], {}, "/")
    False
    """
    normalized = _normalized_argv(argv)
    if not normalized:
        return False

    compiler_name = Path(normalized[0]).name
    if compiler_name == "rustc":
        return _is_query(normalized[1:], has_driver=False)
    if compiler_name in WORKSPACE_DRIVERS:
        return _is_workspace_query(normalized, env, cwd)
    return False


def _is_workspace_query(
    argv: tuple[str, ...], env: cabc.Mapping[str, str], cwd: str
) -> bool:
    """Recognize one query behind a single verified workspace driver.

    Returns
    -------
    bool
        Whether the recognized wrapper and inner rustc have a safe query.
    """
    if len(argv) < WORKSPACE_ARGV_MINIMUM:
        return False
    driver, compiler = argv[:2]
    driver_name = Path(driver).name
    if Path(compiler).name != "rustc":
        return False
    if not _is_recognized_driver(driver, driver_name, env, cwd):
        return False
    if driver_name == "dylint-driver" and _has_dylint_flags(env):
        return False
    return _is_query(argv[2:], has_driver=True)


def _normalized_argv(argv: cabc.Sequence[str | bytes]) -> tuple[str, ...] | None:
    """Normalize OS argv and remove only caller sccache layers.

    Returns
    -------
    tuple[str, ...] | None
        Normalized arguments, or ``None`` when an argument file or bad value
        makes the grammar unsafe to inspect.
    """
    try:
        decoded = tuple(os.fsdecode(argument) for argument in argv)
        if any(argument.startswith("@") for argument in decoded):
            return None
        return compiler_argv(decoded)
    except OSError, TypeError, ValueError:
        return None


def _is_recognized_driver(
    driver: str, driver_name: str, env: cabc.Mapping[str, str], cwd: str
) -> bool:
    """Verify configured driver identity, or use the closed name allow-list.

    Returns
    -------
    bool
        Whether this is one recognized workspace driver.
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
    """Whether Dylint will append extra rustc arguments.

    Returns
    -------
    bool
        Whether non-whitespace Dylint rustflags make the invocation unsafe.
    """
    flags = env.get("DYLINT_RUSTFLAGS", "")
    return not isinstance(flags, str) or bool(flags.strip())


def _is_query(arguments: tuple[str, ...], *, has_driver: bool) -> bool:
    """Accept one complete query grammar and reject all unconsumed tokens.

    Returns
    -------
    bool
        Whether one version, print or Cargo target query matches.
    """
    if not arguments:
        return False
    if _is_version_query(arguments):
        return True
    if _is_print_query(arguments, has_driver=has_driver):
        return True
    return _is_cargo_target_probe(arguments)


def _is_version_query(arguments: tuple[str, ...]) -> bool:
    """Recognize one rustc version flag and optional verbosity marker.

    Returns
    -------
    bool
        Whether the arguments contain exactly one version request.
    """
    versions = [argument for argument in arguments if argument in VERSION_FLAGS]
    verbose_count = arguments.count("--verbose")
    return (
        len(versions) == 1
        and verbose_count <= 1
        and len(arguments) == len(versions) + verbose_count
    )


def _is_print_query(arguments: tuple[str, ...], *, has_driver: bool) -> bool:
    """Recognize print-only argv with no input or output redirection.

    Returns
    -------
    bool
        Whether every argument requests one allowed print value.
    """
    index = 0
    requests = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument.startswith("--print="):
            kind = argument.removeprefix("--print=")
            index += 1
        elif argument == "--print":
            if has_driver:
                return False
            kind = _following_argument(arguments, index)
            if kind is None:
                return False
            index += 2
        else:
            return False
        if kind not in PRINT_KINDS:
            return False
        requests += 1
    return requests > 0
