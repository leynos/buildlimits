"""Fixtures that read a store's rows back at a stated document version.

A test that inspects the events a recorder emitted must read them through the
document the dataset command writes, not through in-memory rows: only the
written document states each event's label-time provenance. These helpers write
and read one document at a named version, so a test that claims a version's
behaviour exercises that version's shapes.
"""

import dataclasses as dc
import tempfile
import typing as typ
from pathlib import Path

import pytest

from buildlimits import outcome_record_codec
from buildlimits.outcome_record_encode import _ROW_FIELDS_BY_VERSION, encode_line
from buildlimits.outcome_record_format import (
    FORMAT_VERSION,
    DecodedStore,
    OutcomeRecord,
    StoreHeader,
    StoreRecord,
)
from buildlimits.predict.accounting import HistoryLoss
from buildlimits.predict.dataset import build_snapshot
from buildlimits.predict.records import decode_history
from buildlimits.predict.snapshot_store import load_snapshot, save_snapshot

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.predict.events import Event


def record_events(
    records: tuple[StoreRecord, ...],
    *,
    now: dt.datetime,
    version: int = FORMAT_VERSION,
) -> tuple[Event, ...]:
    """Snapshot one reading's records and load the events back.

    Parameters
    ----------
    records : tuple[StoreRecord, ...]
        The records one reading emitted, outcome rows and summary included.
    now : datetime
        The moment the reading returned, used as the store's write time.
    version : int
        The document version to write and read back. Each row is stripped to
        the shape that version could hold before it is written.

    Returns
    -------
    tuple[Event, ...]
        The loaded event stream, in store append order.
    """
    header = StoreHeader("fixed-salt", now, version=version)
    document = "\n".join((
        encode_line(header),
        *(
            encode_line(_as_version_row(record, version), version=version)
            for record in records
        ),
    ))
    decoded = outcome_record_codec.decode_lines(document + "\n")
    if not isinstance(decoded, DecodedStore):
        pytest.fail(f"the fixture's own document did not decode: {decoded}")
    snapshot = build_snapshot(
        decode_history(decoded),
        now=now,
        history_loss=HistoryLoss.active_file_only(),
    )
    path = Path(tempfile.mkdtemp()) / "history.jsonl"
    save_snapshot(path, snapshot)
    return load_snapshot(path).events


def _as_version_row(record: StoreRecord, version: int) -> StoreRecord:
    """Reproduce the row shape the named document version actually wrote.

    A test that asks what an older history looks like must clear the evidence
    that release could not carry, because the encoder correctly refuses to
    record proof a document cannot hold. The fields to clear come from the
    encoder's own version table, so a field added later is cleared here without
    this helper being edited.

    Returns
    -------
    StoreRecord
        The record as that version would have written it.
    """
    if not isinstance(record, OutcomeRecord) or version >= FORMAT_VERSION:
        return record
    allowed = _ROW_FIELDS_BY_VERSION[version]
    absent = {
        field.name
        for field in dc.fields(OutcomeRecord)
        if field.name not in allowed and getattr(record, field.name) is not None
    }
    if not absent:
        return record
    return dc.replace(record, **dict.fromkeys(absent))


__all__ = ["record_events"]
