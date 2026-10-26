"""The three collaborators the application service drives, as protocols.

Each port names one thing the tooling needs from outside its own policy: a
checker to run a specification, an inventory to compare the model's mappings
against, and a source to read the model and its configurations from. The
service depends on these and nothing else, so it can be exercised with a
recording, a literal set of paths and a string — no binary, no repository and no
temporary directory.

The protocols are structural. An adapter satisfies one by having the methods,
without importing this module, which is what keeps the dependency arrow
pointing inward: adapters know the domain, and the ports know only the domain.

There is no example block here because a port has nothing to demonstrate: each
body is a docstring, with no ``...``, so the signature *is* the contract. The
tests that use these protocols through a double are the examples.
"""

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck.domain import CheckResult, Configuration, MaterializedSpec


class ModelChecker(typ.Protocol):
    """Run one materialized specification and classify the outcome."""

    def check(self, spec: MaterializedSpec) -> CheckResult:
        """Return the classified result; the adapter owns its scratch directory."""


class CodeInventory(typ.Protocol):
    """Enumerate the production surface the model must map."""

    def items(self) -> frozenset[str]:
        """Return dotted paths of events, effects, phases and fault kinds."""

    def resolves(self, dotted_path: str) -> bool:
        """Report whether a mapped dotted path still names something."""


class ConfigurationSource(typ.Protocol):
    """Supply the model source, its configurations and drift exclusions."""

    def source_text(self) -> str:
        """Return the concatenated model source."""

    def configurations(self) -> tuple[Configuration, ...]:
        """Return every configuration with its expectations."""

    def exclusions(self) -> cabc.Mapping[str, str]:
        """Return inventory items deliberately unmapped, each with its reason."""
