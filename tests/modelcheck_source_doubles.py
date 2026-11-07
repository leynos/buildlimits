"""A configuration source the ``modelcheck`` tests hold as a value.

The scenarios are about verdicts rather than about parsing TOML, so the source
is a value here; ``TomlConfigurations`` has its own tests. It lives apart from
``modelcheck_doubles`` because that module is about stubbing the checker's
*output*, and this one is about the other port: the model text and the
configurations a matrix runs.
"""

import typing as typ

from modelcheck.ports import ConfigurationSource

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck.domain import Configuration


class FixedSource(ConfigurationSource):
    """A configuration source holding values the scenario supplied.

    It subclasses :class:`~modelcheck.ports.ConfigurationSource` so each
    method is an override of a declared port method. Forwarding to a held
    value is the whole point of a double, and the base class is what says the
    indirection is deliberate rather than incidental.
    """

    def __init__(
        self,
        configurations: cabc.Sequence[Configuration] = (),
        *,
        source: str = "",
    ) -> None:
        """Hold the configurations and the source text.

        Parameters
        ----------
        configurations : Sequence[Configuration]
            The configurations the matrix runs, in report order. Empty by
            default: the drift command reads only the model text, and a double
            that exists to raise has no configurations to offer.
        source : str
            The concatenated model source.
        """
        self._source = source
        self._configurations = tuple(configurations)

    @typ.override
    def source_text(self) -> str:
        """Return the concatenated model source.

        Returns
        -------
        str
            The text the drift check reads its mappings from.
        """
        return self._source

    @typ.override
    def configurations(self) -> tuple[Configuration, ...]:
        """Return every configuration with its expectations.

        Returns
        -------
        tuple[Configuration, ...]
            The configurations, in the order they were supplied.
        """
        return self._configurations

    @typ.override
    def exclusions(self) -> cabc.Mapping[str, str]:
        """Return inventory items deliberately unmapped, each with its reason.

        Returns
        -------
        Mapping[str, str]
            No exclusions: a scenario that needs one supplies it through the
            drift values directly.
        """
        return {}
