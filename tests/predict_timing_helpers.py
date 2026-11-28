"""Builders for the cache-outcome timing contract tests.

Each event carries a complete version 5 correlation group, so a test can state
exact decision, completion and acceptance moments and then assert what a later
decision could learn. The helpers are shared by the causal replay tests and the
document round-trip tests, so "verified" has one definition.
"""

import dataclasses as dc
import datetime as dt
import hashlib
import typing as typ

from buildlimits.predict.config_loader import load_config
from buildlimits.predict.dataset import Snapshot, Source
from buildlimits.predict.events import EPOCH, Event

if typ.TYPE_CHECKING:
    from buildlimits.predict.config import Config


def at(seconds: float) -> dt.datetime:
    """Return the epoch plus the supplied seconds.

    Returns
    -------
    datetime
        The moment used by these tests.
    """
    return EPOCH + dt.timedelta(seconds=seconds)


def event(
    index: int,
    *,
    moments: tuple[float | None, float | None],
    label: str = "hit",
    attempt: str | None = None,
) -> Event:
    """Build one event with explicit decision and availability moments.

    The observation time is the availability moment, and the row marks it
    verified, which is what a version 5 recorder writes for a reading it
    accepted. An event with no availability moment is written unverified, so it
    releases nothing.

    Parameters
    ----------
    index : int
        The stream position, which also supplies the default attempt token.
    moments : tuple[float | None, float | None]
        The decision and availability moments in seconds, or None where the
        source recorded none.
    label : str
        The outcome label.
    attempt : str | None
        The attempt token, defaulting to this stream position.

    Returns
    -------
    Event
        The verified event.
    """
    admitted, available = moments
    moment = None if available is None else at(available)
    return Event(
        identity=f"{index:064d}"[:64],
        origin="git",
        admitted_at=None if admitted is None else at(admitted),
        observed_at=moment if moment is not None else at(0),
        label=typ.cast("typ.Any", label),
        runtime_seconds=1.0,
        label_observed_verified=moment is not None,
        completed_at=moment,
        attempt=f"g{index}" if attempt is None else attempt,
        supervisor_generation="generation-a",
        generation_digest=hashlib.sha256(f"gen|{index}".encode()).hexdigest(),
        record_evidence=hashlib.sha256(
            f"evidence|{index}|{moments}|{label}".encode()
        ).hexdigest(),
    )


def reviewer_stream() -> tuple[Event, ...]:
    """Return A, B and the two decisions either side of A's availability.

    Returns
    -------
    tuple[Event, ...]
        The four verified events, in store append order.
    """
    return (
        # A: admitted at 1, completes at 2, attribution accepted at 5.
        event(0, moments=(1.0, 5.0)),
        # B: decided at 3, its own label accepted later.
        event(1, moments=(3.0, 9.0), label="miss"),
        # C: decided at 6, after A became available but B had not.
        event(2, moments=(6.0, 12.0)),
        # D: decided at exactly 5, the availability instant itself.
        event(3, moments=(5.0, 20.0), label="miss"),
    )


def config() -> Config:
    """Return the pinned parameters that make each feature row checkable.

    Returns
    -------
    Config
        Parameters whose load window is long enough to count earlier labels.
    """
    return dc.replace(load_config(), load_window_seconds=60.0)


def snapshot(events: tuple[Event, ...]) -> Snapshot:
    """Return a snapshot holding ``events`` in append order.

    Returns
    -------
    Snapshot
        The redacted snapshot value.
    """
    return Snapshot(
        created_at=at(0),
        source=Source(
            salt_id="timing-salt",
            written_at=at(0),
            records_total=len(events),
            outcome_records=len(events),
            summary_records=0,
        ),
        journal=None,
        events=events,
        gaps=(),
    )
