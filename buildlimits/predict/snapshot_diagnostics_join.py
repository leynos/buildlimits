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

    from buildlimits.predict.accounting import Diagnostics
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
    """Require the diagnostics label count to describe these events.

    Every event is built from one outcome row, so the hit and miss rows the
    diagnostics count are exactly the events carrying a label. A document whose
    events and diagnostics disagree describes no single history.

    Raises
    ------
    ValueError
        If the labelled event count differs from the diagnostics label count.
    """
    labelled = sum(1 for event in events if event.label != "unknown")
    if labelled != diagnostics.store.labels:
        msg = (
            f"{path} records {labelled} labelled events but its diagnostics "
            f"count {diagnostics.store.labels} labels"
        )
        raise ValueError(msg)
