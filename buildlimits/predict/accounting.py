"""Bounded label-loss accounting from retained rows to scored outcomes.

The benchmark's gate only sees events that survived to the snapshot. This
module keeps the counts that explain the journey: which cache results were
recorded, why a window produced no label, which origin and parser class the
request came from, and whether an admission time survived. Every count comes
from a closed vocabulary, and evidence a row does not carry is reported as
``unrecorded`` rather than inferred.

The module is deliberately pure. A store reader hands it decoded rows, and the
snapshot adapter stores the value once, so an event row does not grow with
diagnostic fields it will never use.
"""

import collections
import dataclasses as dc
import typing as typ

from buildlimits.outcome_record_format import OutcomeRecord

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_record_format import StoreRecord

UNRECORDED = "unrecorded"
"""The bucket for evidence a row predates or cannot carry."""

_MAX_MAP_ENTRIES = 64
"""A closed vocabulary is small; a larger map is not a count this report reads."""


@dc.dataclass(frozen=True, slots=True)
class StoreAccounting:
    """What the retained rows say about labels and their loss.

    Attributes
    ----------
    records_total : int
        Every row read, summaries included.
    outcome_records : int
        Per-compilation rows.
    summary_records : int
        Window-summary rows, which carry no compilation identity.
    by_result : Mapping[str, int]
        Outcome rows by the closed cache-result vocabulary.
    by_reason : Mapping[str, int]
        Outcome rows by the recorder's refusal reason, or ``unrecorded``.
    by_attribution : Mapping[str, int]
        Outcome rows by ``solo``, ``unanimous``, ``none`` or ``unrecorded``.
    by_origin : Mapping[str, int]
        Outcome rows by crate origin.
    by_parser_class : Mapping[str, int]
        Outcome rows by the argv parser prediction.
    by_child_sighting : Mapping[str, int]
        Outcome rows by the bounded child-process observation.
    by_server_version : Mapping[str, int]
        Outcome rows by the sccache version in their key.
    admission_recorded : int
        Outcome rows carrying an admission time.
    admission_missing : int
        Outcome rows without an admission time.
    labels : int
        Outcome rows whose result is ``hit`` or ``miss``.
    cacheable_records : int
        Outcome rows the argv parser classified as ``cacheable``, which is the
        denominator a cache-hit rate is measured against.
    cacheable_labels : int
        Cacheable rows whose result is ``hit`` or ``miss``.
    """

    records_total: int
    outcome_records: int
    summary_records: int
    by_result: cabc.Mapping[str, int]
    by_reason: cabc.Mapping[str, int]
    by_attribution: cabc.Mapping[str, int]
    by_origin: cabc.Mapping[str, int]
    by_parser_class: cabc.Mapping[str, int]
    by_child_sighting: cabc.Mapping[str, int]
    by_server_version: cabc.Mapping[str, int]
    admission_recorded: int
    admission_missing: int
    labels: int
    cacheable_records: int
    cacheable_labels: int

    @property
    def labelled_fraction(self) -> float:
        """The labelled share of the per-compilation rows.

        Returns
        -------
        float
            ``labels / outcome_records``, or zero for an empty store.
        """
        return 0.0 if not self.outcome_records else self.labels / self.outcome_records


@dc.dataclass(frozen=True, slots=True)
class HistoryLoss:
    """How much retained history an extraction could and could not see.

    Attributes
    ----------
    active_only : bool
        Whether the command read only the active file, so archived and sealed
        segments were not considered.
    archived : int
        Published XZ archives the inventory listed.
    sealed : int
        Sealed plain segments the inventory listed.
    gaps : int
        Missing segment sequences at or above the retention floor.
    gaps_truncated : bool
        Whether the gap list stopped at its fixed bound.
    retention_floor : int | None
        Lowest sequence intentionally retained, when recorded.
    quarantined : int
        Segments the inventory knows are corrupt or unsupported.
    """

    active_only: bool
    archived: int = 0
    sealed: int = 0
    gaps: int = 0
    gaps_truncated: bool = False
    retention_floor: int | None = None
    quarantined: int = 0

    @classmethod
    def active_file_only(cls) -> HistoryLoss:
        """Return the loss statement for a read of the active file alone.

        Returns
        -------
        HistoryLoss
            A value that says closed history was not considered.
        """
        return cls(active_only=True)


@dc.dataclass(frozen=True, slots=True)
class Diagnostics:
    """The retained-store and history-loss evidence behind one snapshot."""

    store: StoreAccounting
    history: HistoryLoss


def store_accounting(records: cabc.Iterable[StoreRecord]) -> StoreAccounting:
    """Count one decoded store without retaining its rows.

    Parameters
    ----------
    records : Iterable[StoreRecord]
        The decoded store rows, in append order.

    Returns
    -------
    StoreAccounting
        Exact counts with every breakdown summing to ``outcome_records``.
    """
    counters = _DimensionCounters()
    outcomes = summaries = admitted = 0
    cacheable = cacheable_labels = 0
    for record in records:
        if isinstance(record, OutcomeRecord):
            outcomes += 1
            counters.add(record)
            admitted += int(record.admitted_at is not None)
            if record.parser_class == "cacheable":
                cacheable += 1
                cacheable_labels += int(record.outcome in {"hit", "miss"})
        else:
            summaries += 1
    return StoreAccounting(
        records_total=outcomes + summaries,
        outcome_records=outcomes,
        summary_records=summaries,
        by_result=_frozen(counters.result),
        by_reason=_frozen(counters.reason),
        by_attribution=_frozen(counters.attribution),
        by_origin=_frozen(counters.origin),
        by_parser_class=_frozen(counters.parser),
        by_child_sighting=_frozen(counters.sighting),
        by_server_version=_frozen(counters.version),
        admission_recorded=admitted,
        admission_missing=outcomes - admitted,
        labels=counters.result["hit"] + counters.result["miss"],
        cacheable_records=cacheable,
        cacheable_labels=cacheable_labels,
    )


@dc.dataclass(slots=True)
class _DimensionCounters:
    """One accumulator per closed diagnostic dimension."""

    result: collections.Counter[str] = dc.field(default_factory=collections.Counter)
    reason: collections.Counter[str] = dc.field(default_factory=collections.Counter)
    attribution: collections.Counter[str] = dc.field(
        default_factory=collections.Counter
    )
    origin: collections.Counter[str] = dc.field(default_factory=collections.Counter)
    parser: collections.Counter[str] = dc.field(default_factory=collections.Counter)
    sighting: collections.Counter[str] = dc.field(default_factory=collections.Counter)
    version: collections.Counter[str] = dc.field(default_factory=collections.Counter)

    def add(self, record: OutcomeRecord) -> None:
        """Count one outcome row on every dimension."""
        self.result[record.outcome] += 1
        self.reason[record.outcome_reason or UNRECORDED] += 1
        self.attribution[record.outcome_attribution or UNRECORDED] += 1
        self.origin[record.origin] += 1
        self.parser[record.parser_class] += 1
        self.sighting[record.child_sighting] += 1
        self.version[record.key.toolchain_identity.server_version] += 1


def _frozen(counts: collections.Counter[str]) -> dict[str, int]:
    """Return a deterministically ordered plain mapping.

    The map is capped at the loader's entry bound. A real vocabulary is small;
    if a corrupted or hostile stream produced more distinct values, the
    largest counts are kept so the writer can never emit a block its own
    loader would refuse. A cap that bites is visible because the dropped
    entries are no longer reported.

    Returns
    -------
    dict[str, int]
        Counts sorted by key, at most ``_MAX_MAP_ENTRIES`` entries.
    """
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return dict(sorted(ordered[:_MAX_MAP_ENTRIES]))


def checked_counts(value: object, *, label: str) -> dict[str, int]:
    """Validate one decoded counts object from a snapshot document.

    Parameters
    ----------
    value : object
        The decoded JSON value.
    label : str
        The field name used in a refusal.

    Returns
    -------
    dict[str, int]
        The validated counts.

    Raises
    ------
    TypeError
        If the value is not an object of text keys and integer counts.
    ValueError
        If a count is negative or the map exceeds its fixed bound.
    """
    if not isinstance(value, dict):
        msg = f"{label} must be an object of counts"
        raise TypeError(msg)
    if len(value) > _MAX_MAP_ENTRIES:
        msg = f"{label} holds more entries than a closed vocabulary"
        raise ValueError(msg)
    counts: dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(key, str):
            msg = f"{label} must map text keys to integer counts"
            raise TypeError(msg)
        match count:
            case bool():
                msg = f"{label} counts must be non-negative integers"
                raise ValueError(msg)
            case int() if count < 0:
                msg = f"{label} counts must be non-negative integers"
                raise ValueError(msg)
            case int():
                counts[key] = count
            case _:
                msg = f"{label} must map text keys to integer counts"
                raise TypeError(msg)
    return counts
