"""Join a snapshot's diagnostics block to the history it describes.

The snapshot document states the same facts three times: the source block
counts the rows read, the diagnostics block counts them again by dimension, and
the event rows carry the labels. A report prints all three, so a document whose
blocks disagree describes no single history. These checks live apart from the
loader so each refusal stays next to the two values it compares.
"""

import typing as typ

from buildlimits.predict.formatting import as_counts

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.accounting import Diagnostics, StoreAccounting
    from buildlimits.predict.dataset import Source
    from buildlimits.predict.events import Event


def require_diagnostics_match(
    document: object, source: Source, diagnostics: Diagnostics
) -> None:
    """Require the diagnostics block to describe the same store as its source.

    Both blocks state the row totals, and the report prints both. A document
    where they disagree cannot be evidence of any one history, so it is
    refused before either count reaches a reader.

    Raises
    ------
    ValueError
        If the retained-store counts disagree with the source counts.
    """
    store = diagnostics.store
    if (store.records_total, store.outcome_records, store.summary_records) != (
        source.records_total,
        source.outcome_records,
        source.summary_records,
    ):
        msg = f"{document} diagnostics store counts disagree with its source counts"
        raise ValueError(msg)


def require_event_labels_match(
    document: object, events: cabc.Sequence[Event], diagnostics: Diagnostics
) -> None:
    """Require the diagnostics counts to describe these events exactly.

    The writer emits one event per outcome row, so the event stream is the
    history the diagnostics claim to summarize. Matching only the labelled
    total would accept a document whose hit and miss events were swapped, or
    whose admission times were removed, because neither changes that total.
    Every count the report prints from both blocks is compared here, including
    the per-origin tally: changing one event's origin changes what the origin
    table and the origin breakdown report about the same history.

    Raises
    ------
    ValueError
        If the event count, the per-label counts, the per-origin counts or the
        admission presence disagree with the diagnostics block.
    """
    store = diagnostics.store
    if len(events) != store.outcome_records:
        msg = (
            f"{document} holds {len(events)} events but its diagnostics count "
            f"{store.outcome_records} outcome rows"
        )
        raise ValueError(msg)
    _require_per_label(document, events, store.by_result)
    _require_per_origin(document, events, store.by_origin)
    _require_admission_presence(document, events, store)


def _require_per_origin(
    document: object, events: cabc.Sequence[Event], by_origin: cabc.Mapping[str, int]
) -> None:
    """Require each origin's event count to match its diagnostics count.

    The origin vocabulary is closed, so a map comparison is enough: every
    origin either tally names is counted on both sides, and an origin present
    on one side alone counts as zero on the other.

    Raises
    ------
    ValueError
        If an origin's count differs between the events and the block.
    """
    counted: dict[str, int] = {}
    for event in events:
        counted[event.origin] = counted.get(event.origin, 0) + 1
    if counted != {origin: count for origin, count in by_origin.items() if count}:
        msg = (
            f"{document} records its events by origin as {as_counts(counted)} but "
            f"its diagnostics count {as_counts(by_origin)}"
        )
        raise ValueError(msg)


def _require_per_label(
    document: object, events: cabc.Sequence[Event], by_result: cabc.Mapping[str, int]
) -> None:
    """Require each label's event count to match its diagnostics count.

    Raises
    ------
    ValueError
        If a hit or miss count differs between the events and the block.
    """
    for label in ("hit", "miss"):
        counted = sum(1 for event in events if event.label == label)
        if counted != by_result.get(label, 0):
            msg = (
                f"{document} records {counted} {label} events but its diagnostics "
                f"count {by_result.get(label, 0)}"
            )
            raise ValueError(msg)


def _require_admission_presence(
    document: object, events: cabc.Sequence[Event], store: StoreAccounting
) -> None:
    """Require the recorded and missing admission times to match the events.

    An event that carries no admission time is exactly an outcome row whose
    admission was never recorded, so the two sides count the same rows. Whether
    a recorded time is *usable* is a separate question the walk-forward
    schedule answers, and it is deliberately not checked here.

    Raises
    ------
    ValueError
        If the recorded or missing admission counts disagree.
    """
    recorded = sum(1 for event in events if event.admitted_at is not None)
    if recorded != store.admission_recorded:
        msg = (
            f"{document} records {recorded} admission times on its events but its "
            f"diagnostics count {store.admission_recorded}"
        )
        raise ValueError(msg)
    if len(events) - recorded != store.admission_missing:
        msg = (
            f"{document} leaves {len(events) - recorded} events without an "
            f"admission time but its diagnostics count "
            f"{store.admission_missing} missing"
        )
        raise ValueError(msg)
