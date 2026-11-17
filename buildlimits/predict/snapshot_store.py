"""Read and write the versioned cache-outcome history snapshot.

The snapshot document is the benchmark's only input, so loading it validates
every field the report later reads. A hand-edited or truncated file is refused
with the field that is wrong rather than surfacing as a confusing report.
"""

import itertools
import json
import math
import os
import tempfile
import typing as typ
from pathlib import Path

from buildlimits.predict.dataset import Gap, Snapshot, Source
from buildlimits.predict.snapshot_codec import (
    SNAPSHOT_VERSION,
    event_from_dict,
    snapshot_to_dict,
)
from buildlimits.utc_text import utc_moment, utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.predict.events import Event

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
    document = json.dumps(snapshot_to_dict(snapshot), indent=2, sort_keys=True)
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
    decoded_events = tuple(event_from_dict(row) for row in events)
    _require_ordered_clock(path, decoded_events)
    return Snapshot(
        created_at=_parse_stamp(created),
        source=_source_from(path, typ.cast("dict[str, object]", source)),
        journal=_journal_from(path, typ.cast("dict[str, object]", journal)),
        events=decoded_events,
        gaps=_gaps_from(path, document.get("stats")),
    )


def _require_ordered_clock(path: Path, events: cabc.Sequence[Event]) -> None:
    """Refuse a history whose recorded clock ran backwards under admissions.

    The store stamps ``observed_at`` when it appends a record, so a later
    position carrying an earlier stamp means the clock stepped backwards. No
    monotonic sequence is stored beside the times, so the two sides of a step
    cannot be separated: an event admitted later could sort before a label
    observed earlier, and the schedule would predict with a label not yet
    observed. A history carrying admission times that crosses a step is
    refused rather than scored; one with no admission times is unaffected,
    because it is already reported as unpredicted.

    Raises
    ------
    ValueError
        If an admission-bearing history records an earlier observation after a
        later one.
    """
    if not any(event.admitted_at is not None for event in events):
        return
    for earlier, later in itertools.pairwise(events):
        if later.observed_at < earlier.observed_at:
            msg = (
                f"{path} records {utc_text(later.observed_at)} after "
                f"{utc_text(earlier.observed_at)}; admission times cannot order "
                "a history whose clock ran backwards"
            )
            raise ValueError(msg)


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

    The report reads availability, the printed record count, the reason when
    unavailable, and the supervisor-start block. Checking those at load time
    keeps a hand-edited document from failing inside the Markdown formatter,
    where a text count reaches an integer format. An empty note is accepted:
    it means the snapshot target queried no journal.

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
        If the record count is negative.
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
        count = _count(path, "records", records)
        if count < 0:
            msg = f"{path} journal records must not be negative"
            raise ValueError(msg)
    restarts = journal.get("restarts")
    if restarts is not None:
        if not isinstance(restarts, dict):
            msg = f"{path} journal restarts must be an object"
            raise TypeError(msg)
        _validate_restarts(path, typ.cast("dict[str, object]", restarts))
    return journal


def _count(path: Path, name: str, value: object) -> int:
    """Return a count the report would print as a number.

    Returns
    -------
    int
        The count, which the caller checks for a negative value.

    Raises
    ------
    TypeError
        If the value is not an integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{path} journal {name} must be an integer"
        raise TypeError(msg)
    return value


def _validate_restarts(path: Path, value: cabc.Mapping[str, object]) -> None:
    """Validate the supervisor-start block the report renders.

    The report prints the count as a number and the two bounds as text, so a
    hand-edited snapshot naming either in another type is rejected here rather
    than inside the Markdown renderer. A non-null count with a ``first`` bound
    also selects the bounded-range wording, which reads ``last``; without it
    the renderer would fail with a missing key instead of naming the field.

    Raises
    ------
    TypeError
        If a field has the wrong type, or a bounded range omits its last
        bound.
    ValueError
        If the count is negative.
    """
    count = value.get("count")
    starts: int | None = None
    if count is not None:
        starts = _count(path, "restarts count", count)
        if starts < 0:
            msg = f"{path} journal restarts count must not be negative"
            raise ValueError(msg)
    bounds: dict[str, str | None] = {}
    for key in ("first", "last"):
        stamp = value.get(key)
        if stamp is not None and not isinstance(stamp, str):
            msg = f"{path} journal restarts {key} must be text"
            raise TypeError(msg)
        bounds[key] = stamp
    claims_range = starts is not None and bounds["first"] is not None
    if claims_range and bounds["last"] is None:
        msg = f"{path} journal restarts must hold a last bound with first"
        raise TypeError(msg)


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
        If the number is not finite, including an integer too large for a
        float to hold. ``NaN``, the infinities and an overflowing integer are
        not lengths a report can carry, so the gap's field is named instead of
        letting the loader fail on the conversion.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{path} stats gap must hold a numeric seconds"
        raise TypeError(msg)
    try:
        seconds = float(value)
    except OverflowError as error:
        # A JSON integer can be larger than a float can hold. That is a bad
        # field value like any other, not a crash in the snapshot loader.
        msg = f"{path} stats gap must hold finite seconds"
        raise ValueError(msg) from error
    if not math.isfinite(seconds):
        msg = f"{path} stats gap must hold finite seconds"
        raise ValueError(msg)
    return seconds


def _parse_stamp(value: object) -> dt.datetime:
    """Parse a required UTC timestamp.

    Text that is not an offset timestamp, or whose offset leaves the range a
    UTC datetime can hold, is refused as a ``ValueError`` by
    :func:`buildlimits.utc_text.utc_moment`.

    Returns
    -------
    datetime
        The parsed time in UTC.

    Raises
    ------
    TypeError
        If the value is not text.
    """
    if not isinstance(value, str):
        msg = "a snapshot timestamp must be text"
        raise TypeError(msg)
    return utc_moment(value, label="a snapshot timestamp")
