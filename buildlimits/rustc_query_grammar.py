"""Fail-closed argument grammar for non-compiling rustc information queries.

Scope
-----
These parsers are private to ``rustc_query.is_toolchain_query``. Add a query
form only after its pinned compiler behaviour and complete token grammar have
been reviewed; callers must not use these helpers to validate compile argv.
"""

from buildlimits.rustc_cargo_probe import (
    following_argument as _following_argument,
)
from buildlimits.rustc_cargo_probe import (
    is_cargo_target_probe as _is_cargo_target_probe,
)

VERSION_FLAGS = frozenset({"-V", "--version", "-vV"})
PRINT_KINDS = frozenset({
    "sysroot",
    "target-libdir",
    "cfg",
    "target-list",
    "target-cpus",
    "target-features",
})


def is_query(arguments: tuple[str, ...], *, has_driver: bool) -> bool:
    """Accept one complete version, print or Cargo query grammar.

    Parameters
    ----------
    arguments : tuple[str, ...]
        Arguments following the inner rustc executable.
    has_driver : bool
        Whether a workspace driver wraps rustc. Drivers reject the separate-
        value ``--print KIND`` spelling.

    Returns
    -------
    bool
        Whether the complete vector matches one supported query grammar.

    Examples
    --------
    >>> is_query(("-vV",), has_driver=False)
    True
    >>> is_query(("--print", "cfg"), has_driver=True)
    False
    """
    if not arguments:
        return False
    return (
        _is_version_query(arguments)
        or _is_print_query(arguments, has_driver=has_driver)
        or _is_cargo_target_probe(arguments)
    )


def _is_version_query(arguments: tuple[str, ...]) -> bool:
    """Recognize one rustc version flag and optional verbosity marker.

    Returns
    -------
    bool
        Whether exactly one version flag and at most one verbosity marker occur.
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
        Whether every argument is an allow-listed print request.
    """
    index = 0
    while index < len(arguments):
        parsed = _print_query_argument(arguments, index, has_driver=has_driver)
        if parsed is None:
            return False
        kind, index = parsed
        if kind not in PRINT_KINDS:
            return False
    return index > 0


def _print_query_argument(
    arguments: tuple[str, ...], index: int, *, has_driver: bool
) -> tuple[str, int] | None:
    """Parse one allowed print spelling and return its next argv index.

    Returns
    -------
    tuple[str, int] | None
        The print kind and next index, or ``None`` for an unsupported spelling.
    """
    argument = arguments[index]
    if argument.startswith("--print="):
        return argument.removeprefix("--print="), index + 1
    if argument != "--print" or has_driver:
        return None
    kind = _following_argument(arguments, index)
    if kind is None:
        return None
    return kind, index + 2
