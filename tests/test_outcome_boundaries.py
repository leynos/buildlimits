"""Static conformance checks for the pure outcome domain boundary."""

import ast
from pathlib import Path

from import_graph_test_helpers import package_closure

_PACKAGE = Path(__file__).parents[1] / "buildlimits"
_DOMAIN_MODULES = (
    "outcome_vocabulary.py",
    "outcome_stats.py",
    "outcome_attribution.py",
    "outcome_footprints.py",
    "outcome_gate.py",
    "json_values.py",
    "outcome_key.py",
    "outcome_key_arguments.py",
    "outcome_parser.py",
    "outcome_ports.py",
    "outcome_recorder.py",
    "outcome_recorder_outputs.py",
    "outcome_recorder_reading.py",
    "outcome_recorder_types.py",
    "outcome_record_codec.py",
    "outcome_record_format.py",
    "outcome_records.py",
    "outcome_window.py",
)
_DOMAIN_ROOTS = tuple(filename.removesuffix(".py") for filename in _DOMAIN_MODULES)
_FORBIDDEN_IMPORTS = {
    "io",
    "os",
    "socket",
    "subprocess",
    "threading",
    "time",
}
_FORBIDDEN_CALLS = {"input", "open", "print"}
_FORBIDDEN_CLOCK_CALLS = {
    "clock_gettime",
    "monotonic",
    "now",
    "perf_counter",
    "process_time",
    "time",
    "today",
    "utcnow",
}
_ALLOWED_PATHLIB_IMPORTS = {"PurePosixPath"}


def _import_facts(node: ast.AST) -> tuple[set[str], bool]:
    """Return imported module roots and pathlib import purity for one node.

    Examples
    --------
    >>> _import_facts(ast.parse("from pathlib import PurePosixPath").body[0])
    ({'pathlib'}, True)
    >>> _import_facts(ast.parse("import pathlib").body[0])
    ({'pathlib'}, False)
    """
    match node:
        case ast.Import(names=aliases):
            modules = {alias.name.split(".")[0] for alias in aliases}
            return modules, "pathlib" not in modules
        case ast.ImportFrom(module=module, names=aliases, level=level):
            module_root = (module or "").split(".")[0]
            pathlib_import_is_pure = module_root != "pathlib" or (
                level == 0
                and all(
                    alias.name in _ALLOWED_PATHLIB_IMPORTS and alias.asname is None
                    for alias in aliases
                )
            )
            return {module_root}, pathlib_import_is_pure
        case _:
            return set(), True


def _call_target(node: ast.AST) -> tuple[str | None, str | None]:
    """Return a direct or attribute call target from one node.

    Examples
    --------
    >>> _call_target(ast.parse("open('file')").body[0].value)
    ('open', None)
    >>> _call_target(ast.parse("source.read()").body[0].value)
    (None, 'read')
    """
    match node:
        case ast.Call(func=ast.Name(id=name)):
            return name, None
        case ast.Call(func=ast.Attribute(attr=attribute)):
            return None, attribute
        case _:
            return None, None


def _domain_facts(
    tree: ast.Module,
) -> tuple[set[str], bool, set[str], set[str]]:
    """Collect purity-relevant imports and call names in one tree walk.

    Examples
    --------
    >>> _domain_facts(ast.parse("import socket; print('x')"))
    ({'socket'}, True, {'print'}, set())
    """
    imported_modules: set[str] = set()
    pathlib_imports_are_pure = True
    called_names: set[str] = set()
    called_attributes: set[str] = set()
    for node in ast.walk(tree):
        modules, is_pure_pathlib_import = _import_facts(node)
        imported_modules.update(modules)
        pathlib_imports_are_pure &= is_pure_pathlib_import
        name, attribute = _call_target(node)
        if name is not None:
            called_names.add(name)
        if attribute is not None:
            called_attributes.add(attribute)
    return imported_modules, pathlib_imports_are_pure, called_names, called_attributes


def _purity_violations(filename: str) -> list[str]:
    """Report every purity rule one module breaks.

    Returns
    -------
    list[str]
        One line per broken rule, so a failure names all of them at once.
    """
    tree = ast.parse((_PACKAGE / filename).read_text(encoding="utf-8"))
    imports, pathlib_is_pure, calls, attributes = _domain_facts(tree)
    violations: list[str] = []
    if imports & _FORBIDDEN_IMPORTS:
        violations.append(f"imports {sorted(imports & _FORBIDDEN_IMPORTS)}")
    if not pathlib_is_pure:
        violations.append("imports an impure pathlib name")
    if calls & _FORBIDDEN_CALLS:
        violations.append(f"calls {sorted(calls & _FORBIDDEN_CALLS)}")
    if attributes & _FORBIDDEN_CLOCK_CALLS:
        violations.append(
            f"reads the clock through {sorted(attributes & _FORBIDDEN_CLOCK_CALLS)}"
        )
    return violations


def test_every_module_in_the_domain_closure_remains_pure() -> None:
    """Keep I/O, process, thread and clock dependencies outside the domain.

    The closure is checked rather than the listed modules, so a module a
    domain module starts importing cannot escape the same rules.
    """
    for name in sorted(package_closure(_PACKAGE, _DOMAIN_ROOTS)):
        assert not _purity_violations(f"{name}.py"), name


def test_outcome_domain_modules_never_reach_admission() -> None:
    """The outcome domain must not depend on admission decisions (O4)."""
    closure = package_closure(_PACKAGE, _DOMAIN_ROOTS)

    assert set(_DOMAIN_ROOTS) <= closure, "the closure reaches every domain module"
    assert "admission" not in closure, "the domain reaches the admission machine"
