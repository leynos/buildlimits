"""Reconcile the snapshot's two per-generation label-loss maps.

A document states label loss per sccache generation twice: once for every
outcome row and once for the labelled rows within those buckets. The two maps
partition different totals, so neither can be checked by the loop that checks
every other breakdown, and a document where they disagree describes two
different histories. The rules live here rather than in the codec so that
module keeps one responsibility: reading the document's shape.
"""

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

GENERATION_MAPS = frozenset({"by_generation", "labels_by_generation"})
"""The breakdowns that arrived with version 4, so older documents lack them."""

GENERATION_VERSION = 4
"""The first snapshot version whose diagnostics state a per-generation map.

It matches ``snapshot_codec.SNAPSHOT_VERSION``; the constant is repeated rather
than imported because the snapshot codec imports the diagnostics codec, and the
two would otherwise form a cycle.
"""

UNRECORDED = "unrecorded"
"""The bucket for rows, and labels, that no generation can be attributed to."""


def require_generation_labels(
    labelled: cabc.Mapping[str, int],
    totals: cabc.Mapping[str, int],
    labels: int,
    label: str,
) -> None:
    """Require the per-generation labels to describe a possible history.

    A labelled count cannot name a generation the row map does not hold, cannot
    exceed that generation's own row total, and must add up to the labelled
    total the same document states. Any breach means the labelled map and the
    document around it describe different histories, so the block is refused
    rather than printed.

    Parameters
    ----------
    labelled : Mapping[str, int]
        The labelled rows of each generation.
    totals : Mapping[str, int]
        Every outcome row of each generation.
    labels : int
        The document's own labelled total, which the map must sum to.
    label : str
        The document name used in a refusal.

    Raises
    ------
    ValueError
        If a labelled count names an unknown bucket, exceeds its own total, or
        the map does not sum to the document's labelled count.

    Examples
    --------
    A map whose buckets and counts agree with the document is accepted.
    >>> require_generation_labels({"a": 1}, {"a": 2, "b": 1}, 1, "doc")
    """
    unknown = set(labelled) - set(totals)
    if unknown:
        msg = f"{label} generation labels name unknown buckets {sorted(unknown)}"
        raise ValueError(msg)
    if any(count > totals[bucket] for bucket, count in labelled.items()):
        msg = f"{label} generation labels exceed their own generation totals"
        raise ValueError(msg)
    if sum(labelled.values()) != labels:
        msg = f"{label} generation labels do not sum to the labelled count"
        raise ValueError(msg)


def unrecorded_generations(
    outcome_records: int, labels: int
) -> tuple[dict[str, int], dict[str, int]]:
    """Return the two generation maps a pre-version-4 history really states.

    Such a document has no generation evidence, and its rows cannot gain any
    after the fact. Every outcome row it holds is therefore stated as
    unattributed rather than left out of a breakdown that must account for all
    of them, and every labelled row with it: a history that names no generation
    cannot say which generation a label came from either.

    Returns
    -------
    tuple[dict[str, int], dict[str, int]]
        The row map and the labelled map, each reconciling with its own total.
    """
    rows = {UNRECORDED: outcome_records} if outcome_records else {}
    labelled = {UNRECORDED: labels} if labels else {}
    return rows, labelled
