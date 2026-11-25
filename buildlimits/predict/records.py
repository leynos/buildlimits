"""Map the store's recorded outcomes onto redacted benchmark events.

A recorded row becomes an event only when it carries a compilation identity.
The mapping hashes that identity, so neither a crate name nor a path survives
into the snapshot. Only an attributed ``hit`` or ``miss`` becomes a label.

This is the adapter between the decoded store and the history the domain
assembles: :func:`decode_history` reads the store's header counts and hands
back a :class:`~buildlimits.predict.dataset.StoreHistory`, so the dataset
module never imports the store format.
"""

import dataclasses as dc
import hashlib
import json
import typing as typ

from buildlimits.outcome_record_format import OutcomeRecord
from buildlimits.predict.accounting import store_accounting
from buildlimits.predict.dataset import Source, StoreHistory
from buildlimits.predict.events import Event, Label

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_key import CacheKey
    from buildlimits.outcome_record_format import DecodedStore
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

    The admission time is copied only when the row carries one. A version 1
    row, or a launch that never reached admission, has none, and
    ``observed_at - runtime_seconds`` is not a substitute: ``observed_at`` is
    when the label became known, which can be later than the compilation
    finished, so the subtraction only bounds the admission from above. Using
    that bound as the decision time would count labels that arrived after the
    real admission, which is the leak the walk-forward order exists to stop.
    The absence is reported, not repaired.

    The label time is emitted as verified only when the row says so. A
    version 1 or 2 store row cannot say whether its ``observed_at`` was stamped
    by the fixed recorder or by the earlier build that stamped a launch's
    completion, so it proves nothing about when its label was really known. A
    row without that marker decodes its provenance as ``None`` and is emitted
    unverified; treating it as proof would silently release an old label before
    the reading that produced it. The current recorder writes ``verified`` on
    every row it dates at the reading, and that is the one source the benchmark
    can score without withholding.

    Returns
    -------
    Event
        The redacted event, with its recorded admission time when present and
        its label-time provenance copied from the row.
    """
    return Event(
        identity=identity_of(record.key),
        origin=record.origin,
        admitted_at=record.admitted_at,
        observed_at=record.observed_at,
        label=label_of(record.outcome),
        runtime_seconds=record.runtime_seconds,
        label_observed_verified=record.label_time_provenance == "verified",
    )


def build_events(records: cabc.Iterable[object]) -> tuple[Event, ...]:
    """Select attributed records, keeping the store's append order.

    A batch is a run of consecutive records sharing one label time, so append
    order is the order labels were written and is never replaced by a
    timestamp sort: a backward clock step would otherwise move a record into
    an earlier window.

    Returns
    -------
    tuple[Event, ...]
        The event stream in the order the store recorded it.
    """
    return tuple(
        event_of(record) for record in records if isinstance(record, OutcomeRecord)
    )


def decode_history(decoded: DecodedStore) -> StoreHistory:
    """Read one decoded store into the domain's history value.

    Parameters
    ----------
    decoded : DecodedStore
        The store header and every record read from it.

    Returns
    -------
    StoreHistory
        The attributed events, in append order, and the store provenance they
        were read with.
    """
    records = decoded.records
    outcomes = sum(1 for record in records if isinstance(record, OutcomeRecord))
    return StoreHistory(
        events=build_events(records),
        source=Source(
            salt_id=decoded.header.salt_id,
            written_at=decoded.header.written_at,
            records_total=len(records),
            outcome_records=outcomes,
            summary_records=len(records) - outcomes,
            salt_ids=decoded.salt_ids,
        ),
        accounting=store_accounting(records),
    )
