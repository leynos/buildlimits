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
"""

import importlib
import inspect
import typing as typ

from modelcheck.domain import MaterializeError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

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


class BuildlimitsInventory:
    """Enumerate the production paths a model may map.

    Examples
    --------
    The enumeration is of value members, not of the module's whole contents: a
    helper function is not something a model maps.

    >>> "buildlimits.effects.Launch" in BuildlimitsInventory().items()
    True
    >>> BuildlimitsInventory().resolves("buildlimits.effects.Launch")
    True
    >>> BuildlimitsInventory().resolves("buildlimits.effects.NoSuchEffect")
    False
    """

    def __init__(self) -> None:
        """Build the inventory once, on first use.

        The set is cached because a drift check reads it once and a matrix run
        never reads it; building it eagerly in the constructor would make every
        ``check`` run pay for an import it does not use.
        """
        self._items: frozenset[str] | None = None

    def items(self) -> frozenset[str]:
        """Return every dotted path a model may map.

        Returns
        -------
        frozenset[str]
            The effect types, the phases, the fault kinds and the ``Admission``
            event methods, each as ``module.QualName``.

        Raises
        ------
        MaterializeError
            If the production package no longer declares one of the names this
            adapter reads. That is a change to the surface the model was
            written against, and it should fail the check rather than quietly
            reduce the inventory to whatever still imported.
        """  # ruff: ignore[docstring-extraneous-exception] - _enumerated raises it.
        if self._items is None:
            self._items = frozenset(_enumerated())
        return self._items

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


def _enumerated() -> set[str]:
    """Collect every mappable path.

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
    found = {f"{_EFFECTS}.{kind.__name__}" for kind in _effect_types()}
    found.update(f"{_PHASES[0]}.{phase}" for phase in _literals(*_PHASES))
    found.update(f"{_FAULTS[0]}.{fault}" for fault in _literals(*_FAULTS))
    found.update(_events())
    return found


def _effect_types() -> tuple[type, ...]:
    """Return the members of the ``Effect`` alias.

    Returns
    -------
    tuple[type, ...]
        Each effect class the production package can return.

    Raises
    ------
    MaterializeError
        If the alias is missing or no longer holds classes.
    """
    alias = getattr(_module(_EFFECTS), "Effect", None)
    members = typ.get_args(getattr(alias, "__value__", ()))
    if not members or not all(inspect.isclass(member) for member in members):
        msg = f"{_EFFECTS}.Effect is no longer a union of effect classes"
        raise MaterializeError(msg)
    return members


def _literals(module_name: str, attribute: str) -> tuple[str, ...]:
    """Return the members of a ``Literal`` alias the production package declares.

    Parameters
    ----------
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
    >>> _literals(*_FAULTS)[:1]
    ('live_work',)
    """
    alias = getattr(_module(module_name), attribute, None)
    values = typ.get_args(getattr(alias, "__value__", ()))
    if not values or not all(isinstance(value, str) for value in values):
        msg = f"{module_name}.{attribute} is no longer a Literal of strings"
        raise MaterializeError(msg)
    return typ.cast("tuple[str, ...]", values)


def _events() -> set[str]:
    """Return the ``Admission`` methods a model action can drive.

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
    admission = getattr(_module(_ADMISSION), _MODEL, None)
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


def _module(name: str) -> object:
    """Import a module of the production package.

    Parameters
    ----------
    name : str
        The module's dotted name.

    Returns
    -------
    Any
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
        return importlib.import_module(name)
    except ImportError as error:
        msg = f"the production package no longer provides {name}: {error}"
        raise MaterializeError(msg) from error
