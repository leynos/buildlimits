"""Static conformance checks for the pure outcome domain boundary."""

import ast
from pathlib import Path

_PACKAGE = Path(__file__).parents[1] / "buildlimits"
_DOMAIN_MODULES = (
    "outcome_vocabulary.py",
    "outcome_stats.py",
    "outcome_attribution.py",
    "outcome_gate.py",
    "json_values.py",
    "outcome_key.py",
    "outcome_key_arguments.py",
    "outcome_parser.py",
    "outcome_record_codec.py",
    "outcome_record_format.py",
    "outcome_records.py",
    "outcome_window.py",
)
_FORBIDDEN_IMPORTS = {
    "io",
    "os",
    "pathlib",
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
        called_names: set[str] = set()
        called_attributes: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            match node.func:
                case ast.Name(id=name):
                    called_names.add(name)
                case ast.Attribute(attr=attribute):
                    called_attributes.add(attribute)

        assert imported_modules.isdisjoint(_FORBIDDEN_IMPORTS), filename
        assert called_names.isdisjoint(_FORBIDDEN_CALLS), filename
        assert called_attributes.isdisjoint(_FORBIDDEN_CLOCK_CALLS), filename
