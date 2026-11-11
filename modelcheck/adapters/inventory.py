"""Enumerate the production surface the model is supposed to account for.

The model names production code with ``# maps:`` comments, and drift is the
question of whether those names still exist and whether the code has grown
events or effects the model never mapped. Answering it needs a list of paths
the code really has, which means reading ``buildlimits`` reflectively.

Reflection is deliberate. A handwritten list would be a second copy of the
production surface, and it would go stale in exactly the way drift detection
exists to catch: silently, in the direction of a green report. Reading the
package's own enums and dataclasses means adding an effect to ``Effect`` makes
the matrix fail until the model maps it, which is the property the RFC asks
for.

This is the only module in the package allowed to import the production
package; ``tests/test_modelcheck_layering.py`` enforces that, because a second
import would let the tooling depend on the code it is checking.

What is enumerated is the surface a *model* can talk about: the eight effect
types, the five cache-recovery phases, the three drain-fault kinds, and the
``Admission`` methods that return effects. A method that returns anything else
— ``counts``, ``tokens_held``, ``cached_tokens_held`` — is state a model reads
but cannot map an action to, so listing it would demand a mapping no action
could ever satisfy.

Loading is separate from querying. :meth:`BuildlimitsInventory.load` imports
the production modules and is the only step that can touch the filesystem; the
:class:`BuildlimitsInventory` it returns holds the enumerated paths, so
:meth:`BuildlimitsInventory.items` and :meth:`BuildlimitsInventory.resolves`
read a value and import nothing. The import itself is made with bytecode
writing switched off, so a drift check run against a checkout leaves that
checkout exactly as it found it even when the cache is cold.
"""

import contextlib
import importlib
import inspect
import sys
import typing as typ

from modelcheck.domain import MaterializeError
from modelcheck.ports import CodeInventory

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types

__all__ = ["BuildlimitsInventory"]

_EFFECTS = "buildlimits.effects"
"""The module declaring the effect types the model maps."""

_PHASES = ("buildlimits.cache_recovery", "Phase")
"""The alias naming the cache-recovery phases."""

_FAULTS = ("buildlimits.drain_guidance", "FaultKind")
"""The alias naming the drain-fault kinds."""

_ADMISSION = "buildlimits.admission"
"""The module declaring the events a model drive."""

_MODEL = "Admission"
"""The class whose event methods the model maps."""

_MODULES = (_EFFECTS, _PHASES[0], _FAULTS[0], _ADMISSION)
"""The production modules the inventory reads, each named once."""


class BuildlimitsInventory(CodeInventory):
    """Enumerate the production paths a model may map.

    Examples
    --------
    The enumeration is of value members, not of the module's whole contents: a
    helper function is not something a model maps.

    >>> "buildlimits.effects.Launch" in BuildlimitsInventory.load().items()
    True
    >>> BuildlimitsInventory.load().resolves("buildlimits.effects.Launch")
    True
    >>> BuildlimitsInventory.load().resolves("buildlimits.effects.NoSuchEffect")
    False
    """

    def __init__(self, modules: cabc.Mapping[str, types.ModuleType]) -> None:
        """Read the surface from modules the caller has already imported.

        Parameters
        ----------
        modules : collections.abc.Mapping[str, types.ModuleType]
            The production modules to read, keyed by dotted name. Taking them
            as values rather than names is what keeps the query free of
            imports: the caller decides when the production package is loaded,
            and the inventory only reads what it was handed.

        Raises
        ------
        MaterializeError
            If one of the modules no longer declares a name this adapter reads.
            That is a change to the surface the model was written against, and
            it should fail the check rather than quietly reduce the inventory
            to whatever still imported.
        """  # ruff: ignore[docstring-extraneous-exception] - _enumerated raises it.
        self._items = frozenset(_enumerated(modules))

    @classmethod
    def load(cls) -> BuildlimitsInventory:
        """Import the production modules and read the surface from them.

        This is the only step that imports the production package, and the
        composition root calls it once. Everything the returned inventory
        answers afterwards is read from the set built here.

        Callers must not import concurrently with this call. The import runs
        with bytecode writing switched off, and that flag is process-wide
        rather than per-thread, so a second thread importing at the same moment
        would have its own bytecode suppressed too. Writing no bytecode is
        harmless there, but the guarantee this method makes — that a drift
        check leaves the checkout as it found it — is only about this process's
        one load. Calling :meth:`load` before any concurrent work starts keeps
        the flag's lifetime inside the composition boundary.

        Returns
        -------
        BuildlimitsInventory
            The inventory of the production surface as it is now.

        Raises
        ------
        MaterializeError
            If a module the inventory reads cannot be imported, so a rename in
            the production package fails the drift check rather than skipping
            half the inventory.

        Examples
        --------
        >>> inventory = BuildlimitsInventory.load()
        >>> "buildlimits.admission.Admission.submit" in inventory.items()
        True
        """  # ruff: ignore[docstring-extraneous-exception] - _module raises it.
        return cls({name: _module(name) for name in _MODULES})

    @typ.override
    def items(self) -> frozenset[str]:
        """Return every dotted path a model may map.

        Returns
        -------
        frozenset[str]
            The effect types, the phases, the fault kinds and the ``Admission``
            event methods, each as ``module.QualName``.
        """
        return self._items

    @typ.override
    def resolves(self, dotted_path: str) -> bool:
        """Report whether a mapped path still names something.

        Parameters
        ----------
        dotted_path : str
            A path the model wrote in a ``# maps:`` annotation.

        Returns
        -------
        bool
            Whether the path is in the inventory. A mapped path that names a
            helper rather than a mappable surface is not resolving, even if the
            attribute exists: the model is claiming to model something the
            matrix will never run.
        """
        return dotted_path in self.items()


def _enumerated(modules: cabc.Mapping[str, types.ModuleType]) -> set[str]:
    """Collect every mappable path.

    Parameters
    ----------
    modules : collections.abc.Mapping[str, types.ModuleType]
        The production modules to read, keyed by dotted name.

    Returns
    -------
    set[str]
        The paths, before they are frozen.

    Raises
    ------
    MaterializeError
        If a module or alias the inventory reads is gone. The three helpers
        that read the production package raise it.
    """  # ruff: ignore[docstring-extraneous-exception] - its readers raise it.
    found = {f"{_EFFECTS}.{kind.__name__}" for kind in _effect_types(modules)}
    found.update(f"{_PHASES[0]}.{phase}" for phase in _literals(modules, *_PHASES))
    found.update(f"{_FAULTS[0]}.{fault}" for fault in _literals(modules, *_FAULTS))
    found.update(_events(modules))
    return found


def _effect_types(
    modules: cabc.Mapping[str, types.ModuleType],
) -> tuple[type, ...]:
    """Return the members of the ``Effect`` alias.

    Parameters
    ----------
    modules : collections.abc.Mapping[str, types.ModuleType]
        The production modules to read.

    Returns
    -------
    tuple[type, ...]
        Each effect class the production package can return.

    Raises
    ------
    MaterializeError
        If the alias is missing or no longer holds classes.
    """
    alias = getattr(modules[_EFFECTS], "Effect", None)
    members = typ.get_args(getattr(alias, "__value__", ()))
    if not members or not all(inspect.isclass(member) for member in members):
        msg = f"{_EFFECTS}.Effect is no longer a union of effect classes"
        raise MaterializeError(msg)
    return members


def _literals(
    modules: cabc.Mapping[str, types.ModuleType], module_name: str, attribute: str
) -> tuple[str, ...]:
    """Return the members of a ``Literal`` alias the production package declares.

    Parameters
    ----------
    modules : collections.abc.Mapping[str, types.ModuleType]
        The production modules to read.
    module_name : str
        The module declaring the alias.
    attribute : str
        The alias's name.

    Returns
    -------
    tuple[str, ...]
        The literal values, in declaration order.

    Raises
    ------
    MaterializeError
        If the alias is missing or no longer a ``Literal``. Both are read
        through ``__value__`` because a ``type`` statement creates a
        ``TypeAliasType``, and ``typing.get_args`` of that alias is empty.

    Examples
    --------
    >>> _literals({_FAULTS[0]: _module(_FAULTS[0])}, *_FAULTS)[:1]
    ('live_work',)
    """
    alias = getattr(modules[module_name], attribute, None)
    values = typ.get_args(getattr(alias, "__value__", ()))
    if not values or not all(isinstance(value, str) for value in values):
        msg = f"{module_name}.{attribute} is no longer a Literal of strings"
        raise MaterializeError(msg)
    return typ.cast("tuple[str, ...]", values)


def _events(modules: cabc.Mapping[str, types.ModuleType]) -> set[str]:
    """Return the ``Admission`` methods a model action can drive.

    Parameters
    ----------
    modules : collections.abc.Mapping[str, types.ModuleType]
        The production modules to read.

    Returns
    -------
    set[str]
        Each method that returns a list of effects, as
        ``buildlimits.admission.Admission.<name>``. The return type is the
        filter: an event is a call the model makes and then reacts to, and the
        methods that expose state rather than transition it are the ones a
        model reads but cannot map.

    Raises
    ------
    MaterializeError
        If the class itself is gone.
    """
    admission = getattr(modules[_ADMISSION], _MODEL, None)
    if admission is None:
        msg = f"{_ADMISSION}.{_MODEL} is gone, so no model can map events"
        raise MaterializeError(msg)
    return {
        f"{_ADMISSION}.{_MODEL}.{name}"
        for name, member in inspect.getmembers(admission, inspect.isfunction)
        if not name.startswith("_") and _returns_effects(member)
    }


def _returns_effects(member: cabc.Callable[..., object]) -> bool:
    """Report whether a method returns the effect union.

    Parameters
    ----------
    member : collections.abc.Callable[..., object]
        The function to read. ``inspect.getmembers`` pairs ``inspect.isfunction``
        matches with ``object`` keys, so the annotation here is the narrower type
        that filter actually establishes; reading a signature of a bare
        ``object`` is not a question that has an answer.

    Returns
    -------
    bool
        Whether its return annotation is ``list[Effect]`` — ``Effect`` being
        the production package's ``type`` alias, which is what the annotation
        holds rather than the union it stands for. That spelling is how the
        package says "this call has consequences the caller must act on".

    Examples
    --------
    >>> _returns_effects(lambda: None)
    False
    """
    annotation = inspect.signature(member).return_annotation
    return any(
        getattr(argument, "__name__", "") == "Effect"
        for argument in typ.get_args(annotation)
    )


def _module(name: str) -> types.ModuleType:
    """Import a module of the production package.

    Parameters
    ----------
    name : str
        The module's dotted name.

    Returns
    -------
    types.ModuleType
        The imported module.

    Raises
    ------
    MaterializeError
        If the module cannot be imported, so a rename in the production
        package fails the drift check rather than skipping half the inventory.

    Examples
    --------
    >>> _module(_EFFECTS).__name__
    'buildlimits.effects'
    """
    try:
        with _no_bytecode():
            return importlib.import_module(name)
    except ImportError as error:
        msg = f"the production package no longer provides {name}: {error}"
        raise MaterializeError(msg) from error


@contextlib.contextmanager
def _no_bytecode() -> cabc.Generator[None]:
    """Import without writing bytecode beside the sources being read.

    A drift check reads the production package to learn what it declares. The
    compiled copy is not evidence, and writing it would mean the check had
    modified the checkout it was asked to inspect — a cold cache would leave
    ``__pycache__`` directories behind in a tree the maintainer may be about to
    commit. The flag is process-wide, so it is restored rather than left set
    for the rest of the run.

    Yields
    ------
    None
        While the flag is set, so the caller can import inside the block.
    """
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous
