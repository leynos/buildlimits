"""Builders for the cache-outcome timing contract tests.

Each event carries a complete version 3 timing group, so a test can state exact
decision, completion and acceptance moments and then assert what a later
decision could learn. The helpers are shared by the causal replay tests and the
document round-trip tests, so "verified" has one definition.
"""

import dataclasses as dc
import datetime as dt
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


def event(  # ruff: ignore[too-many-arguments]  # one value per event fact; pylint: disable=too-many-arguments
    index: int,
    *,
    admitted: float | None,
    available: float | None,
    label: str = "hit",
    attempt: str | None = None,
) -> Event:
    """Build one event with explicit decision and availability moments.

    The historical observation time equals the availability time, which is what
    a version 3 recorder writes for a row it accepted at one reading.

    Returns
    -------
    Event
        The verified event.
    """
    moment = None if available is None else at(available)
    return Event(
        identity=f"{index:064d}"[:64],
        origin="git",
        admitted_at=None if admitted is None else at(admitted),
        observed_at=moment if moment is not None else at(0),
        label=typ.cast("typ.Any", label),
        runtime_seconds=1.0,
        completed_at=moment,
        label_available_at=moment,
        attempt=f"g{index}" if attempt is None else attempt,
        supervisor_generation="generation-a",
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
        event(0, admitted=1.0, available=5.0),
        # B: decided at 3, its own label accepted later.
        event(1, admitted=3.0, available=9.0, label="miss"),
        # C: decided at 6, after A became available but B had not.
        event(2, admitted=6.0, available=12.0),
        # D: decided at exactly 5, the availability instant itself.
        event(3, admitted=5.0, available=20.0, label="miss"),
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
