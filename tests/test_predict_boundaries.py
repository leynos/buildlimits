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
    "bootstrap",
    "comparison",
    "config",
    "dataset",
    "ensemble_runs",
    "ensembles",
    "evaluation",
    "events",
    "fitting",
    "harness",
    "journal_codec",
    "journal_provenance",
    "metrics",
    "predictors",
    "probability",
    "report",
    "report_codec",
    "scoring",
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
    "buildlimits.predict.paths",
    "buildlimits.predict.provenance",
    "buildlimits.predict.records",
    "buildlimits.predict.snapshot_store",
})


def _imported_modules(path: Path) -> set[str]:
    """Return every module name one source file imports.

    Returns
    -------
    set[str]
        Imported module names, with ``from package import name`` expanded to
        ``package.name`` so an adapter reached by name is still seen.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=names):
                imported.update(alias.name for alias in names)
            case ast.ImportFrom(module=str() as module, names=names):
                imported.add(module)
                parents = module.split(".")
                imported.update(
                    ".".join([*parents, alias.name])
                    for alias in names
                    if alias.name != "*"
                )
    return imported


@pytest.mark.parametrize("name", DOMAIN_MODULES)
def test_a_domain_module_imports_no_infrastructure_or_adapter(name: str) -> None:
    """A domain module depends on values and other domain modules only."""
    path = PACKAGE / f"{name}.py"
    forbidden = _imported_modules(path) & (_INFRASTRUCTURE | _ADAPTERS)

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
