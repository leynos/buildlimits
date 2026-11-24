"""Join a snapshot's diagnostics block to the history it describes.

The snapshot document states the same facts three times: the source block
counts the rows read, the diagnostics block counts them again by dimension, and
the event rows carry the labels. A report prints all three, so a document whose
blocks disagree describes no single history. These checks live apart from the
loader so each refusal stays next to the two values it compares.
"""

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.predict.accounting import Diagnostics, StoreAccounting
    from buildlimits.predict.dataset import Source
    from buildlimits.predict.events import Event


def require_diagnostics_match(
    path: Path, source: Source, diagnostics: Diagnostics
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
        msg = f"{path} diagnostics store counts disagree with its source counts"
        raise ValueError(msg)


def require_event_labels_match(
    path: Path, events: cabc.Sequence[Event], diagnostics: Diagnostics
) -> None:
    """Require the diagnostics counts to describe these events exactly.

    The writer emits one event per outcome row, so the event stream is the
    history the diagnostics claim to summarize. Matching only the labelled
    total would accept a document whose hit and miss events were swapped, or
    whose admission times were removed, because neither changes that total.
    Every count the report prints from both blocks is compared here.

    Raises
    ------
    ValueError
        If the event count, the per-label counts or the admission presence
        disagree with the diagnostics block.
    """
    store = diagnostics.store
    if len(events) != store.outcome_records:
        msg = (
            f"{path} holds {len(events)} events but its diagnostics count "
            f"{store.outcome_records} outcome rows"
        )
        raise ValueError(msg)
    _require_per_label(path, events, store.by_result)
    _require_admission_presence(path, events, store)


def _require_per_label(
    path: Path, events: cabc.Sequence[Event], by_result: cabc.Mapping[str, int]
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
                f"{path} records {counted} {label} events but its diagnostics "
                f"count {by_result.get(label, 0)}"
            )
            raise ValueError(msg)


def _require_admission_presence(
    path: Path, events: cabc.Sequence[Event], store: StoreAccounting
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
            f"{path} records {recorded} admission times on its events but its "
            f"diagnostics count {store.admission_recorded}"
        )
        raise ValueError(msg)
    if len(events) - recorded != store.admission_missing:
        msg = (
            f"{path} leaves {len(events) - recorded} events without an "
            f"admission time but its diagnostics count "
            f"{store.admission_missing} missing"
        )
        raise ValueError(msg)
