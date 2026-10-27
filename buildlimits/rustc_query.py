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
from buildlimits.rustc_query_grammar import is_query
from buildlimits.rustc_query_workspace import is_workspace_query

if typ.TYPE_CHECKING:
    import collections.abc as cabc


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

    if Path(normalized[0]).name == "rustc":
        return is_query(normalized[1:], has_driver=False)
    return is_workspace_query(normalized, env, cwd)


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
