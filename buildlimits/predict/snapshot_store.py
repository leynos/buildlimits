"""Read and write the versioned cache-outcome history snapshot.

The snapshot document is the benchmark's only input, so loading it validates
every field the report later reads. A hand-edited or truncated file is refused
with the field that is wrong rather than surfacing as a confusing report.
"""

import datetime as dt
import json
import math
import os
import tempfile
import typing as typ
from pathlib import Path

from buildlimits.predict.dataset import SNAPSHOT_VERSION, Gap, Snapshot, Source
from buildlimits.predict.events import event_from_dict as _event_from_dict

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_SOURCE_COUNT_FIELDS = ("records_total", "outcome_records", "summary_records")


def save_snapshot(path: Path, snapshot: Snapshot) -> None:
    """Write the snapshot as UTF-8 JSON, replacing any earlier file.

    The document is written to a temporary file in the destination directory
    and moved into place only once it is complete, so a failure part way
    through cannot leave an earlier snapshot truncated. The store rotates, so
    the data behind that earlier snapshot may already be gone; keeping the old
    file is the only way to keep it reproducible.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    document = json.dumps(snapshot.to_dict(), indent=2, sort_keys=True)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(document + "\n")
        temporary.replace(path)
    finally:
        # A successful replace leaves nothing behind, so this only removes the
        # temporary file of a write that failed.
        temporary.unlink(missing_ok=True)


def load_snapshot(path: Path) -> Snapshot:
    """Read and validate a snapshot document.

    The events keep the order the document records, which is the store's
    append order; the loader never re-sorts them by timestamp.

    Returns
    -------
    Snapshot
        The decoded history.

    Raises
    ------
    TypeError
        If the document or one of its values has the wrong Python type.
    ValueError
        If the version is unknown or a field is missing or invalid.
    """
    raw = typ.cast("object", json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        msg = f"{path} must hold one JSON object"
        raise TypeError(msg)
    document = typ.cast("dict[str, object]", raw)
    version = document.get("snapshot_version")
    if isinstance(version, bool) or not isinstance(version, int):
        # JSON `true` and `1.0` both compare equal to the integer 1, so the
        # type is checked before the value or a corrupt version would pass.
        msg = f"{path} must hold an integer snapshot version"
        raise TypeError(msg)
    if version != SNAPSHOT_VERSION:
        msg = f"{path} has an unsupported snapshot version"
        raise ValueError(msg)
    events = document.get("events")
    if not isinstance(events, list):
        msg = f"{path} must hold an events array"
        raise TypeError(msg)
    source = document.get("source")
    journal = document.get("journal")
    created = document.get("created_at")
    if not isinstance(source, dict) or not isinstance(journal, dict):
        msg = f"{path} must hold source and journal objects"
        raise TypeError(msg)
    if not isinstance(created, str):
        msg = f"{path} must hold a created_at timestamp"
        raise TypeError(msg)
    decoded_events = tuple(_event_from_dict(row) for row in events)
    return Snapshot(
        created_at=_parse_stamp(created),
        source=_source_from(path, typ.cast("dict[str, object]", source)),
        journal=_journal_from(path, typ.cast("dict[str, object]", journal)),
        events=decoded_events,
        gaps=_gaps_from(path, document.get("stats")),
    )


def _source_from(path: Path, value: cabc.Mapping[str, object]) -> Source:
    """Rebuild one snapshot's store provenance.

    Returns
    -------
    Source
        The salt, write time and record counts recorded with the snapshot.

    Raises
    ------
    TypeError
        If a field is missing or has the wrong type.
    """
    salt_id = value.get("salt_id")
    written_at = value.get("written_at")
    if not isinstance(salt_id, str) or not isinstance(written_at, str):
        msg = f"{path} source must hold salt_id and written_at text"
        raise TypeError(msg)
    counts = [value.get(name) for name in _SOURCE_COUNT_FIELDS]
    if not all(
        isinstance(count, int) and not isinstance(count, bool) for count in counts
    ):
        msg = f"{path} source must hold integer {', '.join(_SOURCE_COUNT_FIELDS)}"
        raise TypeError(msg)
    records_total, outcome_records, summary_records = typ.cast("list[int]", counts)
    return Source(
        salt_id=salt_id,
        written_at=_parse_stamp(written_at),
        records_total=records_total,
        outcome_records=outcome_records,
        summary_records=summary_records,
    )


def _journal_from(path: Path, value: cabc.Mapping[str, object]) -> dict[str, object]:
    """Validate the journal note the report renders.

    The report reads whether the journal was available, the record count it
    printed, and the reason it printed when it was not. Checking those three
    at load time keeps a hand-edited document from failing inside the Markdown
    formatter, where a count written as text reaches an integer format. An
    empty note is accepted: it means the snapshot target queried no journal.

    Returns
    -------
    dict[str, object]
        The journal note, unchanged.

    Raises
    ------
    TypeError
        If a field the report reads has the wrong type, or an available note
        omits the count it would print.
    ValueError
        If an available note reports a negative count.
    """
    journal = dict(value)
    available = journal.get("available")
    if available is not None and not isinstance(available, bool):
        msg = f"{path} journal must hold a boolean available"
        raise TypeError(msg)
    reason = journal.get("reason")
    if reason is not None and not isinstance(reason, str):
        msg = f"{path} journal reason must be text"
        raise TypeError(msg)
    records = journal.get("records")
    if available and records is None:
        msg = f"{path} available journal must hold a records count"
        raise TypeError(msg)
    if records is not None:
        if isinstance(records, bool) or not isinstance(records, int):
            msg = f"{path} journal records must be an integer"
            raise TypeError(msg)
        if records < 0:
            msg = f"{path} journal records must not be negative"
            raise ValueError(msg)
    return journal


def _gaps_from(path: Path, stats: object) -> tuple[Gap, ...]:
    """Rebuild the reported gaps from a snapshot's stats block.

    Returns
    -------
    tuple[Gap, ...]
        The gaps recorded when the snapshot was taken.

    Raises
    ------
    TypeError
        If the stats block is missing, is not an object, or does not hold a
        list of gap objects.
    """
    if not isinstance(stats, dict):
        msg = f"{path} must hold a stats object"
        raise TypeError(msg)
    reported = typ.cast("dict[str, object]", stats).get("gaps")
    if not isinstance(reported, list):
        msg = f"{path} stats must hold a gaps array"
        raise TypeError(msg)
    gaps = [_gap_from(path, row) for row in reported]
    return tuple(gaps)


def _gap_from(path: Path, row: object) -> Gap:
    """Rebuild one reported gap from its snapshot row.

    Returns
    -------
    Gap
        The silence recorded when the snapshot was taken.

    Raises
    ------
    TypeError
        If the row or one of its fields has the wrong type.
    """
    if not isinstance(row, dict):
        msg = f"{path} stats gaps must hold objects"
        raise TypeError(msg)
    value = typ.cast("dict[str, object]", row)
    start = value.get("start")
    end = value.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        msg = f"{path} stats gap must hold start and end text"
        raise TypeError(msg)
    return Gap(
        start=_parse_stamp(start),
        end=_parse_stamp(end),
        seconds=_seconds_from(path, value.get("seconds")),
    )


def _seconds_from(path: Path, value: object) -> float:
    """Return one gap's length as a float.

    Returns
    -------
    float
        The reported silence in seconds.

    Raises
    ------
    TypeError
        If the value is not a number.
    ValueError
        If the number is not finite. ``NaN`` and the infinities are not valid
        JSON numbers, so carrying one into a report would produce a document
        other readers cannot parse.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{path} stats gap must hold a numeric seconds"
        raise TypeError(msg)
    seconds = float(value)
    if not math.isfinite(seconds):
        msg = f"{path} stats gap must hold finite seconds"
        raise ValueError(msg)
    return seconds


def _parse_stamp(value: object) -> dt.datetime:
    """Parse a required UTC timestamp.

    Returns
    -------
    datetime
        The parsed time in UTC.

    Raises
    ------
    TypeError
        If the value is not text.
    ValueError
        If the text is not a timestamp with an offset.
    """
    if not isinstance(value, str):
        msg = "a snapshot timestamp must be text"
        raise TypeError(msg)
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        msg = "a snapshot timestamp must carry an offset"
        raise ValueError(msg)
    return parsed.astimezone(dt.UTC)
