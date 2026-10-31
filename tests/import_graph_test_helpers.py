"""Resolve the package's own import graph for static boundary tests."""

import ast
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

PACKAGE = "buildlimits"


def _absolute_from(module: str, aliases: list[ast.alias]) -> set[str]:
    """Resolve one absolute ``from`` statement to package module names.

    Returns
    -------
    set[str]
        The package-relative modules the statement names, or an empty set
        when the statement imports from outside the package.
    """
    if module == PACKAGE:
        return {alias.name.split(".")[0] for alias in aliases}
    if module.startswith(f"{PACKAGE}."):
        return {module.split(".")[1]}
    return set()


def _relative_from(module: str | None, aliases: list[ast.alias]) -> set[str]:
    """Resolve one same-package relative ``from`` statement.

    Returns
    -------
    set[str]
        The package-relative modules the statement names.
    """
    if module is not None:
        return {module.split(".")[0]}
    return {alias.name.split(".")[0] for alias in aliases}


def _node_modules(node: ast.AST) -> set[str]:
    """Resolve one import statement to package-relative module names.

    Returns
    -------
    set[str]
        Module names for ``import``, absolute ``from`` and same-package
        relative ``from`` statements; an empty set for anything else.
    """
    match node:
        case ast.Import(names=aliases):
            return {
                parts[1]
                for alias in aliases
                if (parts := alias.name.split("."))[0] == PACKAGE and len(parts) > 1
            }
        case ast.ImportFrom(module=module, names=aliases, level=0) if module:
            return _absolute_from(module, aliases)
        case ast.ImportFrom(module=module, names=aliases, level=1):
            return _relative_from(module, aliases)
        case _:
            return set()


def imported_modules(path: Path) -> set[str]:
    """Return the package modules one source file imports.

    ``if TYPE_CHECKING:`` blocks count, because they are static dependencies
    even when the interpreter never executes them.

    Parameters
    ----------
    path : Path
        A module file inside the package.

    Returns
    -------
    set[str]
        Module names relative to the package, over both ``import`` and
        ``from`` statements.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        names.update(_node_modules(node))
    return names


def package_closure(package: Path, roots: list[str] | tuple[str, ...]) -> set[str]:
    """Return every package module reachable from the given roots.

    Parameters
    ----------
    package : Path
        The package directory holding the flat module files.
    roots : list[str] | tuple[str, ...]
        Package-relative module names to start from.

    Returns
    -------
    set[str]
        The transitive closure, including every root that has a module file.
    """
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        path = package / f"{name}.py"
        if not path.is_file():
            continue
        seen.add(name)
        pending.extend(imported_modules(path) - seen)
    return seen
