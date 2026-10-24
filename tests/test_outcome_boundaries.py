"""Static conformance checks for the pure outcome domain boundary."""

import ast
from pathlib import Path

_PACKAGE = Path(__file__).parents[1] / "buildlimits"
_DOMAIN_MODULES = (
    "outcome_vocabulary.py",
    "outcome_stats.py",
    "outcome_attribution.py",
    "outcome_gate.py",
)
_FORBIDDEN_IMPORTS = {
    "datetime",
    "io",
    "os",
    "pathlib",
    "socket",
    "subprocess",
    "threading",
    "time",
}
_FORBIDDEN_CALLS = {"input", "open", "print"}


def test_outcome_domain_modules_remain_pure() -> None:
    """Keep I/O, process, thread and clock dependencies outside the domain."""
    for filename in _DOMAIN_MODULES:
        tree = ast.parse((_PACKAGE / filename).read_text(encoding="utf-8"))
        imported_modules = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        assert imported_modules.isdisjoint(_FORBIDDEN_IMPORTS), filename
        assert called_names.isdisjoint(_FORBIDDEN_CALLS), filename
