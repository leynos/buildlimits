"""Snapshot documents and histories for the label-loss diagnostics tests.

Three test modules need the same three things: a history decoded from chosen
records, the diagnostics block of an encoded document, and a current document
rewritten into the shape an older release wrote. Keeping them here means a
version bump is stated once, and a test that claims an older document's
behaviour exercises the shape that release really produced.
"""

import datetime as dt
import typing as typ

from outcome_test_helpers import attributed_record
from predict_test_helpers import ACTIVE_FILE_ONLY

from buildlimits.outcome_record_format import DecodedStore, StoreHeader
from buildlimits.predict.dataset import StoreHistory, build_snapshot
from buildlimits.predict.diagnostics_generations import GENERATION_VERSION
from buildlimits.predict.records import decode_history
from buildlimits.predict.snapshot_codec import snapshot_to_dict

__all__ = [
    "NOW",
    "PROVENANCE_VERSION",
    "diagnostics_document",
    "document_as_version",
    "events_of",
    "history_from_records",
    "store_block",
]

NOW = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)
"""The one moment these fixtures date their documents at."""

PROVENANCE_VERSION = 3
"""The first snapshot version whose events state a label-time provenance."""


def history_from_records(*records: object) -> StoreHistory:
    """Decode records into the history value a snapshot is built from.

    Returns
    -------
    StoreHistory
        The decoded history, dated at :data:`NOW`.
    """
    decoded = DecodedStore(StoreHeader("fixed-salt", NOW), typ.cast("typ.Any", records))
    return decode_history(decoded)


def store_block(document: dict[str, object]) -> dict[str, object]:
    """Return the mutable diagnostics store block of one encoded document.

    Returns
    -------
    dict[str, object]
        The block, ready for a test to rewrite a count in place.
    """
    return typ.cast(
        "dict[str, object]",
        typ.cast("dict[str, object]", document["diagnostics"])["store"],
    )


def events_of(document: dict[str, object]) -> list[dict[str, object]]:
    """Return the mutable event rows of one encoded document.

    Returns
    -------
    list[dict[str, object]]
        The rows, ready for a test to add, remove or rewrite one.
    """
    return typ.cast("list[dict[str, object]]", document["events"])


def diagnostics_document() -> dict[str, object]:
    """Return the encoded snapshot of one attributed hit, without a journal.

    Returns
    -------
    dict[str, object]
        A current-version document whose block genuinely reconciles.
    """
    return snapshot_to_dict(
        build_snapshot(
            history_from_records(attributed_record(outcome="hit")),
            now=NOW,
            history_loss=ACTIVE_FILE_ONLY,
        )
    )


def document_as_version(document: dict[str, object], version: int) -> dict[str, object]:
    """Rewrite a current document as the shape the named version wrote.

    A test that loads an older document must reproduce what that release really
    wrote, not a current row with a downgraded header. Version 1 or 2 predates
    the label-time provenance marker, so the marker is dropped with the header;
    version 3 states the marker but predates the per-generation pair, so both
    maps are dropped; the current version states all three, so it keeps them.

    Returns
    -------
    dict[str, object]
        The document, rewritten in place and returned for chaining.
    """
    document["snapshot_version"] = version
    if version < PROVENANCE_VERSION:
        for event in events_of(document):
            event.pop("label_observed_verified", None)
    if version < GENERATION_VERSION:
        store = store_block(document)
        store.pop("by_generation", None)
        store.pop("labels_by_generation", None)
    return document
