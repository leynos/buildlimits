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
        """Return the classified result; the adapter owns its scratch directory.

        Returns
        -------
        CheckResult
            The verdict, or the evidence of a run that proved nothing.

        Raises
        ------
        OSError
            Only if the adapter cannot make its own scratch directory. A
            launcher that is missing or refuses to start is not this: it is an
            ``Errored`` result, because a run that never happened says nothing
            about the model and must be reported beside the runs that did.

        Notes
        -----
        Everything that is the *model's* fault is a value rather than an
        exception, so the matrix can judge it. A raised exception from this
        method therefore means the tooling itself could not run, which is what
        the command line maps to its own exit status.
        """


class CodeInventory(typ.Protocol):
    """Enumerate the production surface the model must map."""

    def items(self) -> frozenset[str]:
        """Return dotted paths of events, effects, phases and fault kinds.

        Returns
        -------
        frozenset[str]
            Every mappable path, as ``module.QualName``.

        Raises
        ------
        MaterializeError
            If the production package cannot be read, or no longer declares a
            name the adapter reads. The inventory is the surface the model was
            written against, so a missing name is a broken check rather than a
            smaller inventory.
        """

    def resolves(self, dotted_path: str) -> bool:
        """Report whether a mapped dotted path still names something.

        Parameters
        ----------
        dotted_path : str
            A path the model wrote in a ``# maps:`` annotation.

        Returns
        -------
        bool
            Whether the path is part of the mappable surface.

        Raises
        ------
        MaterializeError
            For the same reasons as :meth:`items`, which this method reads.
        """


class ConfigurationSource(typ.Protocol):
    """Supply the model source, its configurations and drift exclusions."""

    def source_text(self) -> str:
        """Return the concatenated model source.

        Returns
        -------
        str
            The model's text, in file-name order.

        Raises
        ------
        MaterializeError
            If the model directory holds no source file. A run over no source
            would compile nothing and report a pass over an empty state space.
        OSError
            If the directory or a source file exists but cannot be read.
        """

    def configurations(self) -> tuple[Configuration, ...]:
        """Return every configuration with its expectations.

        Returns
        -------
        tuple[Configuration, ...]
            The configurations, in the order the source declares them. An
            empty result is possible and is refused by the service, which is
            the layer that knows a matrix must judge something.

        Raises
        ------
        MaterializeError
            If a declared value is not one the domain can represent, such as a
            verdict or bound of the wrong shape.
        OSError
            If a configuration file exists but cannot be read.
        """

    def exclusions(self) -> cabc.Mapping[str, str]:
        """Return inventory items deliberately unmapped, each with its reason.

        Returns
        -------
        collections.abc.Mapping[str, str]
            Each excluded path mapped to why it is not modelled. Absence of an
            exclusion table is ordinary and reads as an empty mapping.

        Raises
        ------
        MaterializeError
            If the exclusion table exists but is not a table of strings.
        OSError
            If the configuration file exists but cannot be read.
        """
