"""Map the store's recorded outcomes onto redacted benchmark events.

A recorded row becomes an event only when it carries a compilation identity.
The mapping hashes that identity, so neither a crate name nor a path survives
into the snapshot. Only an attributed ``hit`` or ``miss`` becomes a label.
"""

import dataclasses as dc
import hashlib
import json
import typing as typ

from buildlimits.outcome_record_format import OutcomeRecord
from buildlimits.predict.events import Event, Label, ordered

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_key import CacheKey
    from buildlimits.outcome_vocabulary import CacheResult

_LABEL_BY_OUTCOME: dict[str, Label] = {"hit": "hit", "miss": "miss"}


def identity_of(key: CacheKey) -> str:
    """Hash one cache key so no crate name or path survives in the snapshot.

    Returns
    -------
    str
        A lowercase SHA-256 digest over the key's canonical JSON.
    """
    document = json.dumps(dc.asdict(key), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def label_of(outcome: CacheResult) -> Label:
    """Map one stored cache result onto the benchmark's label vocabulary.

    Only an attributed ``hit`` or ``miss`` is scorable. Every other result,
    including ``unattributed`` and ``declined``, is ``unknown`` and is never
    treated as a miss.

    Returns
    -------
    Label
        ``hit``, ``miss`` or ``unknown``.

    Examples
    --------
    >>> label_of("declined")
    'unknown'
    """
    return _LABEL_BY_OUTCOME.get(outcome, "unknown")


def event_of(record: OutcomeRecord) -> Event:
    """Convert one attributed store record into a benchmark event.

    Returns
    -------
    Event
        The redacted event; admission time is absent because the store does
        not record it.
    """
    return Event(
        identity=identity_of(record.key),
        origin=record.origin,
        admitted_at=None,
        observed_at=record.observed_at,
        label=label_of(record.outcome),
        runtime_seconds=record.runtime_seconds,
    )


def build_events(records: cabc.Iterable[object]) -> tuple[Event, ...]:
    """Select attributed records and order their events by label time.

    Returns
    -------
    tuple[Event, ...]
        The chronological event stream.
    """
    events = (
        event_of(record) for record in records if isinstance(record, OutcomeRecord)
    )
    return ordered(events)
