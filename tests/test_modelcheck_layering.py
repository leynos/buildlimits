"""The pure modules stay pure, and only the inventory adapter sees production.

The domain's value is that it can be trusted without reading files or running a
checker: a judgement is a function of values. That property is easy to lose by
accident — one ``open`` for a convenience loader, one ``subprocess`` for a
quick check — and a reviewer will not catch it in a diff. The rules are
therefore checked by walking the package's abstract syntax tree, which is the
only way to see an import that is never executed in a test.

Each rule is also exercised against a fixture module written to break it. A
rule that cannot reject its own fixture is not a rule: this is the non-vacuity
clause the plan requires, and it is what stops a later edit from narrowing a
check until it can never fail.

These tests carry no EP-M2 red marker. They are structural guards over a
package whose modules already exist in stub form (D17), so there is no
unimplemented function for them to trip over, and marking them red would make
the red run report a strict ``XPASS`` instead of the ``XFAIL`` the plan's
evidence describes (D19).
"""

import ast
from pathlib import Path

import pytest

PURE = (
    "domain",
    "expectations",
    "materialize",
    "frontmatter",
    "drift",
    "trace",
    "report",
    "ports",
    "service",
)
"""The modules with no input, output, clock or production import.

``frontmatter`` renders YAML text from a configuration's already-parsed
options; it reads no file and writes none, so it obeys the same rules as the
materializer that calls it.

``ports`` and ``service`` are pure by construction rather than by accident:
the service reads the model text, the configurations and the checker through
the protocols, so it has nothing to open and no process to start. Listing them
is what keeps that true — a later shortcut that read a file in the service
would be caught here rather than in a review.
"""

ALLOWED = frozenset({"enum", "dataclasses", "typing", "collections", "re", "json"})
"""The standard-library roots a pure module may import.

``collections`` is listed rather than ``collections.abc`` because an import is
walked by root name, and the alias form the house style requires —
``import collections.abc as cabc`` — resolves to ``collections``.
"""

BANNED_NAMES = frozenset({
    "open",
    "read_text",
    "write_text",
    "subprocess",
    "os",
    "time",
})
"""The called or attributed names a pure module must not contain."""

INVENTORY_ADAPTER = "adapters/inventory.py"
"""The one module in the package allowed to import ``buildlimits``."""

PACKAGE = Path(__file__).resolve().parents[1] / "modelcheck"
"""The package under certification."""


def _module_files() -> list[Path]:
    """Return every ``modelcheck`` module.

    Returns
    -------
    list[pathlib.Path]
        The modules, including any adapter that exists.
    """
    return sorted(PACKAGE.rglob("*.py"))


def _relative(path: Path) -> str:
    """Return a module's path from the package root, slash-separated.

    Parameters
    ----------
    path : pathlib.Path
        The module's path.

    Returns
    -------
    str
        The relative path, such as ``adapters/inventory.py``.
    """
    return path.relative_to(PACKAGE).as_posix()


def _tree(path: Path) -> ast.Module:
    """Parse one module.

    Parameters
    ----------
    path : pathlib.Path
        The module to parse.

    Returns
    -------
    ast.Module
        Its abstract syntax tree.
    """
    return ast.parse(path.read_text(encoding="utf-8"))


def imported_roots(tree: ast.Module) -> frozenset[str]:
    """Return the root module name of every absolute import in a tree.

    Parameters
    ----------
    tree : ast.Module
        The module's abstract syntax tree.

    Returns
    -------
    frozenset[str]
        The names imported, ``modelcheck`` included for the package's own
        modules.
    """
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        from_root = _from_root(node)
        if from_root is not None:
            roots.add(from_root)
    return frozenset(roots)


def _from_root(node: ast.AST) -> str | None:
    """Return the root module an absolute ``from`` import names.

    Parameters
    ----------
    node : ast.AST
        A node from the tree.

    Returns
    -------
    str | None
        The root name for ``from a.b import c``, or ``None`` for any other
        node and for a relative import, which names no absolute root.
    """
    if not isinstance(node, ast.ImportFrom):
        return None
    if node.level or not node.module:
        return None
    return node.module.split(".")[0]


def _called_name(func: ast.expr) -> str:
    """Return the name a call expression names.

    Parameters
    ----------
    func : ast.expr
        The call's function expression.

    Returns
    -------
    str
        The attribute name for ``a.b(...)``, the bare name for ``f(...)``, and
        the empty string for a call of a computed expression.
    """
    match func:
        case ast.Name(id=name):
            return name
        case ast.Attribute(attr=attribute):
            return attribute
        case _:
            return ""


def used_names(tree: ast.Module) -> frozenset[str]:
    """Return every name called or attributed in a tree.

    Both a bare call (``open(...)``) and an attribute call
    (``path.read_text(...)``) are recorded, because the rule bans the operation
    however it is spelled. Attribute reads are included too: ``os.environ`` is
    as much of a boundary as ``os.getenv``.

    Parameters
    ----------
    tree : ast.Module
        The module's abstract syntax tree.

    Returns
    -------
    frozenset[str]
        Every called or attributed name.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        match node:
            case ast.Call(func=func):
                names.add(_called_name(func))
            case ast.Attribute(attr=attribute):
                names.add(attribute)
    return frozenset(names)


def violations(tree: ast.Module, *, is_pure: bool) -> list[str]:
    """Return every layering rule a parsed module breaks.

    Parameters
    ----------
    tree : ast.Module
        The module's abstract syntax tree.
    is_pure : bool
        Whether the module must obey the pure-module rules as well as the
        production-import rule every module shares.

    Returns
    -------
    list[str]
        One message per broken rule, empty when the module obeys them all.
    """
    found: list[str] = []
    roots = imported_roots(tree)
    if is_pure:
        # ``buildlimits`` is reported by its own rule below, so it is left out
        # of the allowlist message rather than listed twice.
        found.extend(
            f"imports {root}"
            for root in sorted(roots - ALLOWED - {"modelcheck", "buildlimits"})
        )
        found.extend(
            f"calls {name}" for name in sorted(used_names(tree) & BANNED_NAMES)
        )
    if "buildlimits" in roots:
        found.append("imports buildlimits")
    return found


@pytest.mark.parametrize("module", PURE)
def test_a_pure_module_imports_only_the_allowlist(module: str) -> None:
    """Each pure module's imports are standard-library and internal only.

    The rule is checked on the tree rather than by importing, because
    ``typing.TYPE_CHECKING`` hides imports exactly where a reviewer looks for
    them: an ``import subprocess`` inside that block would pass every runtime
    test and still let the domain spawn a process.
    """
    path = PACKAGE / f"{module}.py"

    assert not violations(_tree(path), is_pure=True), f"{module} breaks a rule"


def test_only_the_inventory_adapter_imports_buildlimits() -> None:
    """The production package is a boundary, reached in exactly one place.

    Every other module sees the production surface as a value: the inventory
    adapter turns it into a ``frozenset`` of dotted paths, and the domain never
    learns that ``buildlimits`` exists. A second import would make the model
    check depend on the code it is checking. The set is allowed to be empty:
    EP-M2 has no adapters yet, and the fixture test below proves the rule still
    rejects an import of its own.
    """
    importers = {
        _relative(path)
        for path in _module_files()
        if "buildlimits" in imported_roots(_tree(path))
    }

    assert importers <= {INVENTORY_ADAPTER}, (
        "only the inventory adapter may import the production package"
    )


@pytest.mark.parametrize(
    ("rule", "source"),
    [
        ("imports os", "import os\n"),
        ("imports subprocess", "import subprocess\n"),
        ("imports pathlib", "import pathlib\n"),
        ("calls open", "def read():\n    return open('x')\n"),
        ("calls read_text", "def read(path):\n    return path.read_text()\n"),
        ("calls write_text", "def write(path):\n    return path.write_text('')\n"),
        ("calls time", "def now(clock):\n    return clock.time()\n"),
        ("imports buildlimits", "import buildlimits\n"),
    ],
)
def test_each_rule_rejects_a_fixture_module(rule: str, source: str) -> None:
    """Every rule rejects a module written to break it.

    A check whose allowlist or ban list had been emptied by a later edit would
    pass the package silently. These fixtures are the proof that each element
    is load-bearing, and each names the rule it breaks so a failure identifies
    which one stopped working.
    """
    found = violations(ast.parse(source), is_pure=True)

    assert rule in found, f"{rule!r} was not reported: {found}"


def test_the_allowlist_names_imports_the_package_uses() -> None:
    """The package's own imports exercise the allowlist, so it is not vacuous.

    An allowlist that named nothing would pass every module; one that named
    everything would fail nothing. That the pure modules' imports intersect the
    allowlist, and add nothing outside it, is the middle ground the rule needs.
    """
    used: set[str] = set()
    for module in PURE:
        used |= imported_roots(_tree(PACKAGE / f"{module}.py"))

    assert used & ALLOWED, "the pure modules import something the allowlist names"
    assert not (used - ALLOWED - {"modelcheck"}), "and nothing outside it"


def test_the_banned_names_are_absent_from_every_pure_module() -> None:
    """No pure module opens, reads, writes, spawns or reads the clock.

    The allowlist already excludes the modules these operations come from, but
    a name can arrive through an alias the allowlist does not see — a builtin
    such as ``open``, or a method on a path passed in from outside. Checking
    the names as well catches both spellings.
    """
    offenders = {
        module: sorted(used_names(_tree(PACKAGE / f"{module}.py")) & BANNED_NAMES)
        for module in PURE
    }

    assert not {module: names for module, names in offenders.items() if names}, (
        "no pure module calls a banned name"
    )
