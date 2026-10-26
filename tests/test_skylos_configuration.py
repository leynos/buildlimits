"""Keep narrow Skylos allowances tied to real collaborator dispatch."""

import ast
import tomllib
from pathlib import Path

import pytest

EXPECTED_ENTRYPOINTS = {
    "buildlimits.groups.Groups.remove": "buildlimits/groups.py",
    "buildlimits.nonblocking_child.NonBlockingChild.poll": (
        "buildlimits/nonblocking_child.py"
    ),
    "buildlimits.nonblocking_child.NonBlockingChild._read_available": (
        "buildlimits/nonblocking_child.py"
    ),
    "buildlimits.outcome_replay.H2Report.as_dict": "buildlimits/outcome_replay.py",
    "buildlimits.outcome_replay.H3Report.as_dict": "buildlimits/outcome_replay.py",
}


def test_skylos_exceptions_are_exact_and_name_existing_methods() -> None:
    """Pin every collaborator-dispatch exception to one real method and file."""
    repository = Path(__file__).resolve().parents[1]
    with (repository / "pyproject.toml").open("rb") as config_file:
        config = tomllib.load(config_file)

    entries = config["tool"]["skylos"]["dead_code"]["entrypoints"]
    expected_paths = set(EXPECTED_ENTRYPOINTS.values())
    scoped_entries = [
        entry for entry in entries if expected_paths.intersection(entry.get("path", ()))
    ]
    actual = {
        (entry["type"], full_name, path)
        for entry in scoped_entries
        for full_name in entry["full_name"]
        for path in entry["path"]
    }
    expected = {("method", name, path) for name, path in EXPECTED_ENTRYPOINTS.items()}
    assert actual == expected, "Skylos exceptions stay exact and file-scoped"

    for entry in scoped_entries:
        assert set(entry) == {"type", "full_name", "path", "reason"}, (
            "each exception contains only its exact selector and explanation"
        )
        assert entry["reason"].strip(), "every exception explains its live call path"
        assert "*" not in entry["full_name"][0], "wildcard symbols are not allowed"
        path = repository / entry["path"][0]
        module = ast.parse(path.read_text(encoding="utf-8"))
        _, class_name, method_name = entry["full_name"][0].rsplit(".", maxsplit=2)
        class_node = next(
            (
                node
                for node in module.body
                if isinstance(node, ast.ClassDef) and node.name == class_name
            ),
            None,
        )
        assert class_node is not None, f"{class_name} exists in {path}"
        assert any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
            for node in class_node.body
        ), f"{class_name}.{method_name} exists in {path}"


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("launched", (object(), "group")),
        ("emptied", (object(),)),
        ("on_tick", (1.0,)),
        ("close", (1.0,)),
    ],
)
def test_null_shell_callbacks_remain_inert(
    method: str, arguments: tuple[object, ...]
) -> None:
    """Confirm underscore-prefixed protocol arguments remain safely ignored."""
    from buildlimits.supervisor_outcome_setup import NullOutcomeShell

    assert getattr(NullOutcomeShell, method)(*arguments) is None, (
        f"the null shell leaves {method} inert"
    )
