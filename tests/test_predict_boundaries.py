"""The prediction domain imports no infrastructure or command modules.

The benchmark's scoring depends on values, not on where they were read from:
the store, the parameter file and the journal are decoded by adapters, and the
commands own the filesystem. This test reads each domain module's imports as
syntax, so a new file-system, JSON or command import fails here rather than
only in review.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "buildlimits" / "predict"

DOMAIN_MODULES = (
    "accounting",
    "bootstrap",
    "comparison",
    "config",
    "dataset",
    "diagnostics_codec",
    "ensemble_runs",
    "ensembles",
    "evaluation",
    "events",
    "fitting",
    "harness",
    "journal_codec",
    "journal_provenance",
    "metrics",
    "observer_accounting",
    "predictors",
    "probability",
    "report",
    "report_codec",
    "scoring",
    "snapshot_diagnostics_join",
)

# Modules that reach outside the process: reading and writing files, parsing
# JSON, running commands, or reading the host clock through the journal query.
_INFRASTRUCTURE = frozenset({
    "argparse",
    "json",
    "os",
    "pathlib",
    "shutil",
    "socket",
    "subprocess",
    "sys",
    "tempfile",
    "buildlimits.metrics_query",
})

# Adapters and commands that decode the outside world for the domain. A domain
# module importing one would put the dependency the wrong way round.
_ADAPTERS = frozenset({
    "buildlimits.predict.benchmark_cli",
    "buildlimits.predict.config_loader",
    "buildlimits.predict.dataset_cli",
    "buildlimits.predict.dataset_report",
    "buildlimits.predict.paths",
    "buildlimits.predict.provenance",
    "buildlimits.predict.records",
    "buildlimits.predict.report_writer",
    "buildlimits.predict.snapshot_codec",
    "buildlimits.predict.snapshot_store",
})


def _absolute(module: str | None, level: int, package: str) -> str:
    """Resolve one ``from`` target against the importing module's package.

    ``level`` counts the package parts the import walks back: zero is
    absolute, one names the module's own package, and each further level
    removes one rightmost part. A bare relative import names the package
    itself, which the caller expands through the imported names.

    Returns
    -------
    str
        The absolute module name, or the empty string for an absolute import
        that names no module.
    """
    if level == 0:
        return module or ""
    parts = package.split(".")
    base = parts[: len(parts) - (level - 1)]
    if module:
        base.append(module)
    return ".".join(base)


def _imported_modules(source: str, module: str) -> set[str]:
    """Return every module name one source text imports.

    Parameters
    ----------
    source : str
        The module source to read.
    module : str
        The dotted name of the module the source defines, so a relative
        import is resolved against its package before it is compared with
        the forbidden names.

    Returns
    -------
    set[str]
        Imported module names, with ``from package import name`` expanded to
        ``package.name`` so an adapter reached by name is still seen, and
        relative forms resolved to the absolute name they reach.
    """
    tree = ast.parse(source, filename=module)
    package = module.rpartition(".")[0]
    imported: set[str] = set()
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=names):
                imported.update(alias.name for alias in names)
            case ast.ImportFrom(module=module_name, names=names, level=level):
                base = _absolute(module_name, level, package)
                if not base:
                    continue
                imported.add(base)
                imported.update(
                    f"{base}.{alias.name}" for alias in names if alias.name != "*"
                )
    return imported


def _forbidden_imports(source: str, module: str) -> set[str]:
    """Return the imported names that reach infrastructure or an adapter.

    A forbidden module is matched by its exact name or as the head of a dotted
    name, so ``import os.path`` is caught by ``os`` even though the exact set
    holds no bare ``os``. The same rule sees a submodule of an adapter, which a
    domain module must not reach any more than the adapter itself.

    Parameters
    ----------
    source : str
        The module source to read.
    module : str
        The dotted name of the module the source defines.

    Returns
    -------
    set[str]
        The imported names that are, or descend from, a forbidden module.
    """
    return {
        name
        for name in _imported_modules(source, module)
        if any(
            name == banned or name.startswith(f"{banned}.")
            for banned in _INFRASTRUCTURE | _ADAPTERS
        )
    }


@pytest.mark.parametrize("name", DOMAIN_MODULES)
def test_a_domain_module_imports_no_infrastructure_or_adapter(name: str) -> None:
    """A domain module depends on values and other domain modules only."""
    path = PACKAGE / f"{name}.py"
    source = path.read_text(encoding="utf-8")
    forbidden = _forbidden_imports(source, f"buildlimits.predict.{name}")

    assert not forbidden, (
        f"{path.name} imports {sorted(forbidden)}; decode or write outside the "
        "domain and pass the value in"
    )


def test_every_listed_domain_module_exists() -> None:
    """The boundary list names real modules, so a rename cannot skip a check."""
    missing = [
        name for name in DOMAIN_MODULES if not (PACKAGE / f"{name}.py").is_file()
    ]

    assert not missing, f"the boundary list names missing modules {missing}"


def test_relative_adapter_imports_are_resolved_before_they_are_compared() -> None:
    """Both relative forms reach an adapter, so both must be named absolutely."""
    source = (
        "from .config_loader import load_config\n"
        "from . import snapshot_store\n"
        "from ..admission import Admission\n"
    )

    imported = _imported_modules(source, "buildlimits.predict.dataset")

    assert imported == {
        "buildlimits.admission",
        "buildlimits.admission.Admission",
        "buildlimits.predict",
        "buildlimits.predict.config_loader",
        "buildlimits.predict.config_loader.load_config",
        "buildlimits.predict.snapshot_store",
    }, "every relative form is resolved to the absolute name it reaches"
    assert imported & _ADAPTERS == {
        "buildlimits.predict.config_loader",
        "buildlimits.predict.snapshot_store",
    }, "the adapters are found by their absolute names"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "import os.path as host_path\n",
            {"os.path"},
            id="dotted-infrastructure",
        ),
        pytest.param(
            "from os import path as host_path\n",
            {"os", "os.path"},
            id="named-infrastructure",
        ),
        pytest.param(
            "from . import report_writer\n",
            {"buildlimits.predict.report_writer"},
            id="relative-adapter",
        ),
        pytest.param(
            "from .report_writer import write_report\n",
            {
                "buildlimits.predict.report_writer",
                "buildlimits.predict.report_writer.write_report",
            },
            id="relative-adapter-name",
        ),
        pytest.param("from . import metrics\n", set(), id="domain-module"),
    ],
)
def test_a_forbidden_module_is_matched_by_name_or_descendant(
    source: str, expected: set[str]
) -> None:
    """A dotted spelling of a forbidden module still reaches that module."""
    found = _forbidden_imports(source, "buildlimits.predict.dataset")

    assert found == expected, (
        "every forbidden module is matched by its name or a dot-prefixed "
        "descendant, and a sibling domain module is not"
    )
