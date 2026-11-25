"""Read the nested blocks of one report document as named values.

The report is one mapping of mappings, and more than one renderer reads the
same paths into it. Unpacking the document once into a value means a renderer
cannot re-derive a block by a slightly different path, and a new renderer reads
a typed value rather than repeating the casts the report builder's shape
already fixes.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass(frozen=True, slots=True)
class ReportData:
    """The snapshot blocks a renderer reads.

    Attributes
    ----------
    data : dict[str, object]
        The whole snapshot block, for the fields not unpacked below.
    stats : dict[str, object]
        The events, labels, identities, span and admission counts.
    source : dict[str, object]
        The salt identifier, write time and row counts.
    journal : dict[str, object]
        Availability, counts, span and the observer block.
    diagnostics : object
        The label-loss block, or None for a snapshot that carries none. The
        caller narrows it, because absence is one of the states it reports.
    """

    data: dict[str, object]
    stats: dict[str, object]
    source: dict[str, object]
    journal: dict[str, object]
    diagnostics: object


def report_data(document: cabc.Mapping[str, object]) -> ReportData:
    """Unpack the snapshot blocks one report document carries.

    Returns
    -------
    ReportData
        The whole data block, its stats, source and journal, and the optional
        diagnostics.
    """
    data = typ.cast("dict[str, object]", document["data"])
    return ReportData(
        data=data,
        stats=typ.cast("dict[str, object]", data["stats"]),
        source=typ.cast("dict[str, object]", data["source"]),
        journal=typ.cast("dict[str, object]", data["journal"]),
        diagnostics=data.get("diagnostics"),
    )
