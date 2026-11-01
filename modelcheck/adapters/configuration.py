"""Read a model directory's source, configurations and drift exclusions.

A model directory holds three things: the ``.fizz`` text, a ``fizz.yaml`` of
default bounds, and a ``configurations.toml`` naming each variant the matrix
runs. This adapter reads all three and turns them into the domain's values. It
is the only place that knows the file layout, so the service and the report
never learn that a configuration came from TOML.

The TOML schema is the tooling's own, because the plan fixes the *concept* of a
configuration rather than a file format. Each table is one configuration:

.. code-block:: toml

    [configurations.head]
    min_states = 1000
    holds = ["S1", "S2"]
    reachable = ["StopIssued"]

    [configurations.head.fairness]
    F1 = "weak"

    [configurations.head.constants]
    CAPACITY = "3"

    [configurations.head.options]
    max_actions = 400
    deadlock_detection = true

    [configurations.incident-1-cap3-weak]
    min_states = 1000
    fails_as = { L3 = "lasso" }

An option the file omits is omitted deliberately: ``options`` is rendered into
the frontmatter as a whole, so a variant never inherits a bound by accident.
That is also why ``fizz.yaml`` is read and published rather than merged — the
checker merges frontmatter *over* the YAML, so a variant that restated only one
option would otherwise silently keep the rest.

Drift exclusions live in the same file, under ``[exclusions]``, because a
deliberate omission and a configuration are both statements the maintainer
makes about this model, and a reviewer looking for one should find the other.
"""

import tomllib
import typing as typ

from modelcheck.adapters.configuration_fields import configuration
from modelcheck.domain import Configuration, MaterializeError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ["TomlConfigurations"]

_CONFIGURATIONS = "configurations.toml"
"""The file naming every variant the matrix runs."""

_MODEL_SUFFIX = ".fizz"
"""What a model directory must hold at least one of.

The W1 model is split in two, and each half names its own file, so the suffix
rather than a file name is what the directory is required to contain.
"""


class TomlConfigurations:
    """Read one model directory and turn it into the domain's values.

    Parameters
    ----------
    directory : pathlib.Path
        The model directory.
    bounds : str
        Which declared bound set applies, ``routine`` or ``nightly``. The
        numbers live in the model's ``fizz.yaml``; this adapter only records
        which set the run asked for, so a configuration could later select a
        bound by name without the tooling holding a second copy of it.

    Examples
    --------
    A directory that does not hold the model is reported as a configuration
    defect, naming the file, rather than as a bare ``OSError``.

    >>> from pathlib import Path
    >>> TomlConfigurations(Path("/nonexistent"), "routine").source_text()
    Traceback (most recent call last):
    ...
    modelcheck.domain.MaterializeError: /nonexistent holds no .fizz file
    """

    def __init__(self, directory: Path, bounds: str = "routine") -> None:
        """Record the directory and the bound set.

        Parameters
        ----------
        directory : pathlib.Path
            The model directory.
        bounds : str
            Which declared bound set applies.
        """
        self._directory = directory
        self._bounds = bounds
        self._document: dict[str, object] | None = None

    def source_text(self) -> str:
        """Return the model's source, concatenated in file-name order.

        A model may be one file. It may not be zero: a run with no source
        would compile nothing and report a pass over an empty state space.

        Returns
        -------
        str
            The text of every ``.fizz`` file in the directory, joined with a
            newline so that a file lacking a trailing one does not run into the
            next file's first line.

        Raises
        ------
        MaterializeError
            If the directory holds no ``.fizz`` file. A run with no source
            would compile nothing and report a pass over an empty state space.
        """
        files = sorted(self._directory.glob("*.fizz"))
        if not files:
            msg = f"{self._directory} holds no {_MODEL_SUFFIX} file"
            raise MaterializeError(msg)
        return "\n".join(path.read_text(encoding="utf-8") for path in files)

    def configurations(self) -> tuple[Configuration, ...]:
        """Return every configuration the directory declares.

        Returns
        -------
        tuple[Configuration, ...]
            The configurations, in the order the file declares them, which is
            the order a report shows.

        Raises
        ------
        MaterializeError
            If the file is missing or a table cannot be read as a
            configuration. Every failure names the configuration, because a
            bare TOML error would not say which variant the reader must look
            at. The table readers in
            :mod:`modelcheck.adapters.configuration_fields` raise it for a
            field of the wrong shape.
        """  # ruff: ignore[docstring-extraneous-exception] - the table readers raise it.
        tables = self._table("configurations")
        return tuple(
            configuration(name, typ.cast("dict[str, object]", table))
            for name, table in tables.items()
        )

    def exclusions(self) -> cabc.Mapping[str, str]:
        """Return the inventory paths the model deliberately does not map.

        Returns
        -------
        collections.abc.Mapping[str, str]
            Each excluded dotted path and the reason it was excluded. The
            reason is required: an exclusion without one is a forgotten path
            wearing a considered one's clothes.
        """
        return {path: str(reason) for path, reason in self._table("exclusions").items()}

    def _table(self, name: str) -> dict[str, object]:
        """Return one top-level table from ``configurations.toml``.

        Parameters
        ----------
        name : str
            The table's name.

        Returns
        -------
        dict[str, object]
            The table, or an empty one when the file or the table is absent.
            An absent table is not an error here: a model with no drift
            exclusions is normal, and a model with no configurations is caught
            by the service, which refuses an empty matrix.

        Raises
        ------
        MaterializeError
            If the file exists but cannot be parsed, or the table is not a
            table.
        """
        document = self._read()
        table = document.get(name, {})
        if not isinstance(table, dict):
            msg = f"{self._path} has a {name!r} key that is not a table"
            raise MaterializeError(msg)
        return table

    def _read(self) -> dict[str, object]:
        """Read and cache the TOML document.

        Returns
        -------
        dict[str, object]
            The parsed file, or an empty document when there is no file.

        Raises
        ------
        MaterializeError
            If the file exists but is not valid TOML.
        """  # ruff: ignore[docstring-extraneous-exception] - _parsed raises it.
        if self._document is None:
            self._document = self._parsed()
        return self._document

    def _parsed(self) -> dict[str, object]:
        """Parse the file, naming it in any error.

        Returns
        -------
        dict[str, object]
            The parsed document, or an empty one when there is no file.

        Raises
        ------
        MaterializeError
            If the file exists but cannot be read as TOML, naming the file and
            the parser's own complaint so the reader knows which one to fix.
        """
        try:
            with self._path.open("rb") as stream:
                return tomllib.load(stream)
        except FileNotFoundError:
            return {}
        except tomllib.TOMLDecodeError as error:
            msg = f"{self._path} is not valid TOML: {error}"
            raise MaterializeError(msg) from error

    @property
    def _path(self) -> Path:
        """The configuration file's path."""
        return self._directory / _CONFIGURATIONS
