"""Bounded counts from the recorder's own journal events.

The journal is the only source that can show the observer's own view of label
loss: how many requests were admitted, which cache path they took, which
outcomes the recorder published, how many rows the writer dropped and why, and
how many readings it refused or skipped. The counts live apart from the
provenance value they are attached to because they are a measurement of their
own, folded from a closed vocabulary rather than read from a document.
"""

import collections
import dataclasses as dc
import typing as typ

from buildlimits.predict.accounting import UNRECORDED

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.journal_provenance import JournalRecord

__all__ = ["ObserverAccounting", "observer_accounting"]

# Each event's counted dimensions, pairing a schema field with the map it
# feeds. The tables keep the counting declarative: adding a dimension means
# adding a row, and every map is created from the same field list.
_EVENT_DIMENSIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "request_admitted": (
        ("cache_path", "by_cache_path"),
        ("cache_backend", "by_backend"),
    ),
    "cache_outcome": (
        ("cache_result", "by_result"),
        ("outcome_reason", "by_reason"),
        ("outcome_attribution", "by_attribution"),
        ("crate_origin", "by_origin"),
    ),
    "outcome_reading": (
        ("gate_reason", "by_gate_reason"),
        ("outcome_reason", "by_reading_reason"),
    ),
}
# Each event's scalar counters, pairing a schema field with the total it adds
# to. ``request_admitted`` and ``cache_outcome`` state one per event rather
# than a field, so their entries name no field and add one.
_EVENT_COUNTERS: dict[str, tuple[tuple[str | None, str], ...]] = {
    "request_admitted": ((None, "requests"),),
    "cache_outcome": ((None, "outcomes"),),
    "outcome_reading": (
        ("readings", "readings"),
        ("readings_skipped", "readings_skipped"),
        ("readings_failed", "readings_failed"),
    ),
}
_COUNT_FIELDS = (
    "requests",
    "outcomes",
    "store_records",
    "store_discarded",
    "record_omitted",
    "readings",
    "readings_skipped",
    "readings_failed",
)
"""The scalar totals the observer block states.

``store_records`` counts appended rows only, and ``store_discarded`` counts
rows a result dropped. A compaction reports the rows it retained and a seal
the rows it released, but neither appends a row, so counting them would report
a row more than once. They are kept apart in the per-operation map instead.
"""

_MAP_FIELDS = (
    "by_cache_path",
    "by_backend",
    "by_result",
    "by_reason",
    "by_attribution",
    "by_origin",
    "by_store_operation",
    "by_store_reason",
    "by_omission_reason",
    "by_gate_reason",
    "by_reading_reason",
)
"""The closed-vocabulary maps the observer block states."""


def _count_store_discard(
    by_store: collections.Counter[str], fields: cabc.Mapping[str, str]
) -> None:
    """Add one writer result's discarded rows to the store-reason breakdown.

    Only a result that actually dropped a row contributes to this map.
    Counting the ``none`` reason of a clean append would let the breakdown sum
    past the discarded total, and reconciling with that total is the map's
    whole point. The discarded count, not the event count, is added, so one
    aggregated result that evicted many rows still agrees with the total.
    """
    discarded = _integer(fields.get("store_discarded"))
    if discarded:
        by_store[_reason_bucket(fields.get("store_reason"))] += discarded


def _store_record_total(
    fields: cabc.Mapping[str, str],
) -> tuple[str, int]:
    """Return one writer result's operation and the rows it truly appended.

    Only ``append`` adds a row to the store. A ``compact`` reports the rows it
    retained and a ``seal`` the rows it released into a closed segment; neither
    wrote a row the store did not already hold, so summing their ``store_records``
    as appends would count the same row several times over. The operation is
    returned so its own map can record the result, and the returned total is
    zero for any operation but ``append``.

    Returns
    -------
    tuple[str, int]
        The operation token, and the rows appended by it.
    """
    operation = fields.get("store_operation") or "unrecorded"
    if operation != "append":
        return operation, 0
    return operation, _integer(fields.get("store_records"))


@dc.dataclass(frozen=True, slots=True)
class ObserverAccounting:
    """Bounded counts from the recorder's own journal events.

    Every map comes from a closed vocabulary. A count is present only when the
    event that carries it was in the queried window, so a truncated journal is
    reported as truncated rather than read as a zero.

    Attributes
    ----------
    requests : int
        ``request_admitted`` events the window held.
    by_cache_path : Mapping[str, int]
        Admitted requests by ``cached``, ``direct`` or ``direct_fallback``.
    by_backend : Mapping[str, int]
        Admitted requests by cache backend name.
    outcomes : int
        ``cache_outcome`` events the window held.
    by_result : Mapping[str, int]
        Published outcomes by cache result.
    by_reason : Mapping[str, int]
        Published outcomes by refusal reason.
    by_attribution : Mapping[str, int]
        Published outcomes by attribution kind.
    by_origin : Mapping[str, int]
        Published outcomes by crate origin.
    record_omitted : int
        Published outcomes that produced no store row, because the launch had
        no cache key or no wall-clock label moment. An omitted outcome is
        measured loss the writer never sees, so it is counted here from the
        event that carried the reason.
    by_omission_reason : Mapping[str, int]
        Those omitted outcomes by their closed reason. It sums to
        ``record_omitted``, so published outcomes reconcile as stored rows
        plus omitted ones by cause.
    store_records : int
        Rows the writer reported appended. Only an ``append`` result counts;
        the rows a compaction retained or a seal released were written once and
        are not appends.
    store_discarded : int
        Records the writer reported dropped or evicted.
    by_store_operation : Mapping[str, int]
        Writer results by the closed ``StoreOperation``, so the append total can
        be read beside the compactions and seals that also reported rows.
    by_store_reason : Mapping[str, int]
        Dropped or evicted rows by the writer's closed ``StoreReason``, whose
        values include ``expired`` for retention loss and ``save_failed``,
        ``seal_failed``, ``corrupt`` and ``version_mismatch`` for failed
        writes. It sums to ``store_discarded``, so the two kinds of loss stay
        distinguishable without either being inferred.
    readings : int
        Completed statistics readings.
    readings_skipped : int
        Due gates the recorder did not read at.
    readings_failed : int
        Readings that ended without a label.
    by_gate_reason : Mapping[str, int]
        Gate transitions by the clause that held the gate closed.
    by_reading_reason : Mapping[str, int]
        Reading transitions by their outcome reason.
    """

    requests: int = 0
    by_cache_path: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    by_backend: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    outcomes: int = 0
    by_result: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    by_reason: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    by_attribution: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    by_origin: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    record_omitted: int = 0
    by_omission_reason: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    store_records: int = 0
    store_discarded: int = 0
    by_store_operation: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    by_store_reason: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    readings: int = 0
    readings_skipped: int = 0
    readings_failed: int = 0
    by_gate_reason: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    by_reading_reason: cabc.Mapping[str, int] = dc.field(default_factory=dict)

    @classmethod
    def from_parts(
        cls,
        counts: cabc.Mapping[str, int],
        maps: cabc.Mapping[str, cabc.Mapping[str, int]],
    ) -> ObserverAccounting:
        """Build the value from the scalar totals and the closed-vocabulary maps.

        The fold and the journal decoder both assemble this value from the same
        two field families, so the mapping from those families to the fields
        lives here once. A new count is then named in one place rather than in
        both readers.

        Parameters
        ----------
        counts : Mapping[str, int]
            Every scalar total, keyed by its field name.
        maps : Mapping[str, Mapping[str, int]]
            Every closed-vocabulary map, keyed by its field name.

        Returns
        -------
        ObserverAccounting
            The value with every named field set.
        """
        return cls(
            requests=counts["requests"],
            outcomes=counts["outcomes"],
            record_omitted=counts["record_omitted"],
            store_records=counts["store_records"],
            store_discarded=counts["store_discarded"],
            readings=counts["readings"],
            readings_skipped=counts["readings_skipped"],
            readings_failed=counts["readings_failed"],
            by_cache_path=maps["by_cache_path"],
            by_backend=maps["by_backend"],
            by_result=maps["by_result"],
            by_reason=maps["by_reason"],
            by_attribution=maps["by_attribution"],
            by_origin=maps["by_origin"],
            by_omission_reason=maps["by_omission_reason"],
            by_store_operation=maps["by_store_operation"],
            by_store_reason=maps["by_store_reason"],
            by_gate_reason=maps["by_gate_reason"],
            by_reading_reason=maps["by_reading_reason"],
        )


def observer_accounting(
    records: cabc.Iterable[JournalRecord],
) -> ObserverAccounting:
    """Count the recorder's own bounded events in one journal window.

    The maps are summed from the closed vocabularies the events already carry.
    A periodic ``outcome_reading`` summary reports the counts accumulated since
    the previous summary and then resets them, so its counters are summed
    across summaries while its transition reasons are counted separately; the
    two grains never double-count each other.

    Parameters
    ----------
    records : Iterable[JournalRecord]
        The validated records the query returned.

    Returns
    -------
    ObserverAccounting
        Exact counts for the events the window held. The store total counts
        append results only, and an outcome the recorder published without
        storing it is counted by its closed omission reason.
    """
    maps: dict[str, collections.Counter[str]] = {
        name: collections.Counter() for name in _MAP_FIELDS
    }
    counters: dict[str, int] = dict.fromkeys(_COUNT_FIELDS, 0)
    for record in records:
        fields = record.fields
        for field, bucket in _EVENT_DIMENSIONS.get(record.event, ()):
            _count(maps[bucket], fields.get(field))
        for field, bucket in _EVENT_COUNTERS.get(record.event, ()):
            counters[bucket] += 1 if field is None else _integer(fields.get(field))
        if record.event == "cache_outcome":
            _count_omission(maps["by_omission_reason"], fields)
        if record.event == "outcome_store":
            _count_store_result(maps, counters, fields)
    ordered = {name: dict(sorted(counts.items())) for name, counts in maps.items()}
    # The omitted total is the sum of its reason map rather than a separately
    # incremented counter, so the two cannot drift apart. An event that carries
    # no omission field (one an older recorder wrote) adds nothing to either,
    # which keeps absence from reading as a measured zero.
    counters["record_omitted"] = sum(ordered["by_omission_reason"].values())
    return ObserverAccounting.from_parts(counters, ordered)


def _count_omission(
    by_reason: collections.Counter[str], fields: cabc.Mapping[str, str]
) -> None:
    """Count one published outcome that produced no store row.

    An outcome stored normally reports ``none`` and adds nothing, so the map
    sums to ``record_omitted`` instead of counting every stored outcome under a
    reason of its own. A malformed or absent value is treated the same way: the
    event says nothing about an omission, so there is none to count.
    """
    reason = fields.get("record_omitted")
    if reason not in {None, "", "none"}:
        by_reason[typ.cast("str", reason)] += 1


def _count_store_result(
    maps: dict[str, collections.Counter[str]],
    counters: dict[str, int],
    fields: cabc.Mapping[str, str],
) -> None:
    """Fold one writer result into the store totals and its operation map.

    ``store_records`` counts appended rows only, so a compaction's retained
    rows and a seal's released rows are not added to it a second time. The
    operation map still records every result, so a reader can see the
    compactions and seals beside the appends rather than losing them.
    """
    operation, appended = _store_record_total(fields)
    counters["store_records"] += appended
    counters["store_discarded"] += _integer(fields.get("store_discarded"))
    maps["by_store_operation"][operation] += 1
    _count_store_discard(maps["by_store_reason"], fields)


def _count(counts: collections.Counter[str], value: object) -> None:
    """Count one present closed-vocabulary value.

    An event that omits a field is not counted as a value; a malformed value
    cannot occur at the validated journal boundary, so it is ignored rather
    than named as a category.
    """
    if isinstance(value, str) and value:
        counts[value] += 1


def _reason_bucket(value: object) -> str:
    """Return one closed-vocabulary store reason, or the unrecorded bucket.

    A result that dropped rows is evidence whether or not it named a reason, so
    an absent or empty reason becomes ``unrecorded`` rather than losing its
    count. That keeps the breakdown summing to the discarded total.

    Returns
    -------
    str
        The reason, or the unrecorded bucket for an absent one.
    """
    return value if isinstance(value, str) and value else UNRECORDED


def _integer(value: object) -> int:
    """Read one validated non-negative counter from a journal field.

    Returns
    -------
    int
        The counter, or zero when the field is absent or malformed.
    """
    try:
        return max(0, int(typ.cast("str", value)))
    except TypeError, ValueError:
        return 0
