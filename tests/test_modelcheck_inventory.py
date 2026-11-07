"""The inventory reads the production package, and reads it as a model would.

What matters here is *what* the adapter enumerates. A handwritten list would
be a second copy of the production surface and would go stale in the direction
of a green report, so the adapter reflects. These tests therefore assert
against the production package itself rather than against a literal: if
``Effect`` gains a member and this test still passes, the adapter has stopped
reading the package.

The negative cases matter as much. ``Admission.counts`` is a public method that
a model reads but cannot map an action to, so enumerating it would demand a
mapping no action could satisfy. A test that only checked the eight effects
were present would pass an implementation that also listed every accessor.
"""

import inspect
import typing as typ

import pytest

from buildlimits import cache_recovery, drain_guidance, effects
from modelcheck.adapters.inventory import BuildlimitsInventory


def _inventory() -> frozenset[str]:
    """Return the inventory's paths.

    Returns
    -------
    frozenset[str]
        Every dotted path the adapter enumerates.
    """
    return BuildlimitsInventory().items()


def _alias_members(alias: object) -> tuple[object, ...]:
    """Return the members of a production ``type`` alias, untested.

    Parameters
    ----------
    alias : object
        The alias, as the production module declares it.

    Returns
    -------
    tuple[object, ...]
        Its members. A ``type`` statement creates a ``TypeAliasType``, which is
        itself neither iterable nor a ``Literal``; the members live in its
        ``__value__``, which is where the adapter reads them too.
    """
    return typ.get_args(getattr(alias, "__value__", ()))


def _classes(alias: object) -> tuple[type, ...]:
    """Return the classes a production alias holds.

    Parameters
    ----------
    alias : object
        An alias whose members are expected to be classes, such as ``Effect``.

    Returns
    -------
    tuple[type, ...]
        The member classes.
    """
    return tuple(member for member in _alias_members(alias) if inspect.isclass(member))


def _literals(alias: object) -> tuple[str, ...]:
    """Return the strings a production ``Literal`` alias holds.

    Parameters
    ----------
    alias : object
        An alias whose members are expected to be strings, such as ``Phase``.

    Returns
    -------
    tuple[str, ...]
        The member strings.
    """
    return tuple(member for member in _alias_members(alias) if isinstance(member, str))


def test_every_effect_type_is_enumerated() -> None:
    """The eight effect classes are the model's action consequences."""
    expected = {
        f"buildlimits.effects.{member.__name__}" for member in _classes(effects.Effect)
    }

    assert expected <= _inventory(), "every effect class is enumerated"


def test_every_cache_recovery_phase_is_enumerated() -> None:
    """A phase the model cannot name is a state it cannot hold."""
    expected = {
        f"buildlimits.cache_recovery.{phase}"
        for phase in _literals(cache_recovery.Phase)
    }

    assert expected <= _inventory(), "every phase is enumerated"


def test_every_drain_fault_kind_is_enumerated() -> None:
    """A fault kind the model cannot name is a fault it cannot model."""
    expected = {
        f"buildlimits.drain_guidance.{kind}"
        for kind in _literals(drain_guidance.FaultKind)
    }

    assert expected <= _inventory(), "every fault kind is enumerated"


def test_the_event_methods_are_the_ones_returning_effects() -> None:
    """An event is a call the model makes and then reacts to.

    The return type is the filter, and it is read from the production class
    rather than listed here: a new event method is enumerated the moment it
    exists, and an accessor that returns a count is not.
    """
    from buildlimits.admission import Admission

    expected = {
        f"buildlimits.admission.Admission.{name}"
        for name, member in inspect.getmembers(Admission, inspect.isfunction)
        if not name.startswith("_")
        and "Effect" in str(inspect.signature(member).return_annotation)
    }

    assert expected <= _inventory(), "every event method is enumerated"
    assert "buildlimits.admission.Admission.submit" in expected, (
        "the filter finds at least one event, so the first assertion can fail"
    )


@pytest.mark.parametrize("name", ["counts", "tokens_held", "cached_tokens_held"])
def test_an_accessor_is_not_enumerated(name: str) -> None:
    """State a model reads is not an action it can map.

    Listing these would make drift demand a ``# maps:`` annotation no action
    could carry, and the only fix a maintainer had would be an exclusion —
    which is what the inventory exists to stop being necessary.
    """
    assert f"buildlimits.admission.Admission.{name}" not in _inventory(), (
        "an accessor is state, not an action a model can map"
    )


def test_a_path_the_package_has_resolves() -> None:
    """A real path resolves, and a fabricated one does not."""
    inventory = BuildlimitsInventory()

    assert inventory.resolves("buildlimits.effects.Launch"), "a real path resolves"
    assert not inventory.resolves("buildlimits.effects.NoSuchEffect"), (
        "a fabricated path does not resolve"
    )


@pytest.mark.parametrize(
    "path",
    [
        "buildlimits.effects.RequestId",
        "buildlimits.effects.GroupId",
        "buildlimits.effects.Exit",
        "buildlimits.effects.Effect",
        "buildlimits.drain_guidance.FaultKind",
    ],
)
def test_a_name_that_exists_but_is_not_a_member_does_not_resolve(path: str) -> None:
    """Resolution means "the model may map this", not "the attribute exists".

    ``effects.RequestId`` and ``effects.GroupId`` are real names in the
    production module — they are the payload types an effect carries — but they
    are not things a model maps. A resolution check that only looked the
    attribute up would let a model claim to model them, and drift would then
    report the claim as sound.
    """
    inventory = BuildlimitsInventory()

    assert _exists(path), f"{path} should exist for this test to mean anything"
    assert not inventory.resolves(path), "the name exists but is not mappable"


def _exists(path: str) -> bool:
    """Report whether a dotted path names a real attribute.

    Parameters
    ----------
    path : str
        The dotted path to test.

    Returns
    -------
    bool
        Whether the module holds an attribute of that name.
    """
    module_name, _, attribute = path.rpartition(".")
    module = __import__(module_name, fromlist=[attribute])
    return hasattr(module, attribute)


def test_the_inventory_is_built_once() -> None:
    """The set is cached, because a matrix run never reads it and a drift does once."""
    inventory = BuildlimitsInventory()

    assert inventory.items() is inventory.items(), "the second read is the cached set"


def test_the_inventory_is_frozen() -> None:
    """A caller cannot mutate the production surface it was handed."""
    assert isinstance(_inventory(), frozenset), "the surface is immutable"
