"""Validate capped native journal records at the fixed source-trust boundary.

This pure adapter decodes JSON, admits trusted supervisor and self-reported
adapter events, and retains only schema-owned measurements and timestamps.
It performs no journal I/O; metrics_query owns transport and the raw-record cap.
"""

import dataclasses
import datetime as dt
import json
import typing as typ

from buildlimits.metrics import EVENTS, decode_native_fields

if typ.TYPE_CHECKING:
    import collections.abc as cabc

SUPERVISOR_UNIT = "buildlimits-supervisor.service"


class MetricsQueryError(Exception):
    """The journal query failed or its output could not be read."""


@dataclasses.dataclass(frozen=True, slots=True)
class Record:
    """A validated record, without identifiers or raw journal messages.

    Attributes
    ----------
    source : str
        Trusted supervisor or self-reported adapter source.
    event : str
        A catalogue event appropriate to its source.
    fields : Mapping[str, str]
        Validated, unprefixed schema fields.
    timestamp : datetime | None
        Journal receive time in UTC, if valid.
    """

    source: str
    event: str
    fields: cabc.Mapping[str, str]
    timestamp: dt.datetime | None


def of_event(records: cabc.Sequence[Record], event: str) -> tuple[Record, ...]:
    """Select the records of one catalogue event, keeping their order.

    Parameters
    ----------
    records : Sequence[Record]
        Validated records, in the order they were read.
    event : str
        The catalogue event to keep.

    Returns
    -------
    tuple[Record, ...]
        The matching records in their original order.

    Examples
    --------
    >>> rows = [
    ...     Record("supervisor", "pool_failure", {}, None),
    ...     Record("supervisor", "health_changed", {}, None),
    ... ]
    >>> [row.event for row in of_event(rows, "pool_failure")]
    ['pool_failure']
    """
    return tuple(record for record in records if record.event == event)


def present_samples(records: cabc.Sequence[Record], field: str) -> list[float]:
    """Read present validated numeric samples without replacing absence by zero.

    Parameters
    ----------
    records : Sequence[Record]
        Validated records, in the order they were read.
    field : str
        The numeric schema field to collect.

    Returns
    -------
    list[float]
        Present numeric observations in record order.

    Examples
    --------
    >>> rows = [
    ...     Record("supervisor", "admitted", {"queued": "2"}, None),
    ...     Record("supervisor", "admitted", {}, None),
    ... ]
    >>> present_samples(rows, "queued")
    [2.0]
    """
    return [float(record.fields[field]) for record in records if field in record.fields]


def _decode(lines: cabc.Sequence[str], source: str) -> tuple[Record, ...]:
    """Validate capped JSON lines before filtering sources.

    Returns
    -------
    tuple[Record, ...]
        Accepted bounded records.

    Raises
    ------
    MetricsQueryError
        If any journal line is malformed or not a JSON object.
    """
    records = []
    for line in lines:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            msg = "user-journal query returned malformed JSON"
            raise MetricsQueryError(msg) from error
        if not isinstance(raw, dict):
            msg = "user-journal query returned a non-object record"
            raise MetricsQueryError(msg)
        record = _record(raw)
        if record is not None and source in {"all", record.source}:
            records.append(record)
    return tuple(records)


def _source_accepts_event(source: object, event: str, unit: object) -> bool:
    """Check source and unit permissions for an already validated event.

    Returns
    -------
    bool
        Whether the source may supply this event under the fixed trust rules.
    """
    if source == "supervisor":
        return unit == SUPERVISOR_UNIT and event != "adapter_finished"
    return source == "adapter" and event == "adapter_finished"


def _record(raw: dict[str, object]) -> Record | None:
    """Accept the source and copy only validated schema fields.

    Returns
    -------
    Record | None
        The bounded record, or no record for an untrusted source or schema.
    """
    source, event = raw.get("BUILD_LIMITS_SOURCE"), raw.get("BUILD_LIMITS_EVENT")
    if raw.get("BUILD_LIMITS_SCHEMA") != "1" or not isinstance(event, str):
        return None
    if event not in EVENTS:
        return None
    if not _source_accepts_event(source, event, raw.get("_SYSTEMD_USER_UNIT")):
        return None
    fields = decode_native_fields(raw)
    return Record(
        typ.cast("str", source),
        event,
        fields,
        _timestamp(raw.get("__REALTIME_TIMESTAMP")),
    )


def _timestamp(value: object) -> dt.datetime | None:
    """Read the journal receive timestamp without retaining malformed values.

    Returns
    -------
    datetime | None
        A UTC timestamp or no timestamp.
    """
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromtimestamp(int(value) / 1_000_000, tz=dt.UTC)
    except ValueError, OverflowError, OSError:
        return None
