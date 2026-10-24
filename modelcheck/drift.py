"""Find model actions whose production counterpart has moved or vanished.

RFC 0001's mechanical drift check is deliberately weak: it cannot show that a
transcription is *right*, only that nothing in the production surface has been
added or renamed silently. Each model action carries a ``# maps: <dotted
path>`` comment naming the code it transcribes. Comparing that set with the
code's own inventory finds three things:

- an inventory item with neither a mapping nor a recorded exclusion, which is
  a production surface the model does not account for;
- a mapped path that no longer resolves, which is a rename or a deletion the
  model has not followed;
- the deliberate exclusions, carried into the report so a reader can tell a
  considered gap from a forgotten one.

The comparison is a pure function of three values: the mapped paths, the
inventory and a resolving predicate. That keeps :func:`drift` testable without
importing ``buildlimits``, and lets the inventory adapter supply the real
production surface only at the edge.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_MAPS = "# maps:"
"""The annotation whose path a model action claims to transcribe."""


@dc.dataclass(frozen=True, slots=True)
class DriftReport:
    """What one comparison of the model and the code found.

    Attributes
    ----------
    unmapped : tuple[str, ...]
        Inventory items with neither a mapping nor an exclusion, sorted.
    stale : tuple[str, ...]
        Mapped paths that no longer resolve, sorted.
    excluded : tuple[str, ...]
        Inventory items deliberately left unmapped, sorted. Carried rather
        than subtracted so the report can show them.
    """

    unmapped: tuple[str, ...]
    stale: tuple[str, ...]
    excluded: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        """Report whether the model and the code still correspond.

        Returns
        -------
        bool
            Whether there is neither an unmapped item nor a stale path. A
            deliberate exclusion does not make the report dirty.

        Examples
        --------
        >>> DriftReport((), (), ("buildlimits.effects.Exit",)).is_clean
        True
        """
        return not self.unmapped and not self.stale


def parse_mappings(source: str) -> frozenset[str]:
    """Read every ``# maps:`` annotation out of a model's source text.

    The annotation is a FizzBee comment, so it may follow any indentation and
    any amount of space after the colon. An annotation with no path names
    nothing: treating it as the empty path would report a stale mapping for
    ``""``.

    Parameters
    ----------
    source : str
        The specification, with its annotations.

    Returns
    -------
    frozenset[str]
        Every dotted path named, deduplicated.

    Examples
    --------
    >>> sorted(parse_mappings("action Gate:  # maps: a.B.c"))
    ['a.B.c']
    """
    return frozenset(
        path
        for line in source.splitlines()
        if (path := _mapping_path(line)) is not None
    )


def _mapping_path(line: str) -> str | None:
    """Return the dotted path one line's ``# maps:`` annotation names.

    Parameters
    ----------
    line : str
        One line of the model's source.

    Returns
    -------
    str | None
        The path, or ``None`` when the line carries no non-empty annotation.
    """
    _, marker, annotation = line.partition(_MAPS)
    if not marker:
        return None
    return annotation.strip() or None


def drift(
    mapped: frozenset[str],
    inventory: frozenset[str],
    resolves: cabc.Callable[[str], bool],
    exclusions: cabc.Mapping[str, str],
) -> DriftReport:
    """Compare what the model claims to transcribe with the code that exists.

    Parameters
    ----------
    mapped : frozenset[str]
        The dotted paths the model's actions claim to transcribe.
    inventory : frozenset[str]
        The production surface the model must account for.
    resolves : collections.abc.Callable[[str], bool]
        Report whether a mapped path still names something.
    exclusions : collections.abc.Mapping[str, str]
        Inventory items deliberately left unmapped, each with its reason.

    Returns
    -------
    DriftReport
        The unmapped and stale paths, and the exclusions carried alongside.

    Every inventory item lands in exactly one of three accounts: mapped,
    excluded or unmapped. An exclusion is the more specific record, so it wins
    over a mapping and the path drops out of the mapped account altogether —
    otherwise an item could be both mapped and excluded, and a report that
    says "exactly one" would not be checkable. Exclusions are carried as they
    were given, including one for an item the inventory no longer holds: that
    still says the model deliberately does not cover the path.

    Examples
    --------
    >>> drift(frozenset({"a.B"}), frozenset({"a.B", "a.C"}), lambda path: True, {})
    DriftReport(unmapped=('a.C',), stale=(), excluded=())
    """
    deliberate = frozenset(exclusions)
    return DriftReport(
        unmapped=tuple(sorted(inventory - mapped - deliberate)),
        stale=tuple(sorted(path for path in mapped - deliberate if not resolves(path))),
        excluded=tuple(sorted(deliberate)),
    )
