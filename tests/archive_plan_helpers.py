"""Inventory entries and policies shared by the archive planner tests.

The planner is pure, so a test can describe a store without touching a
filesystem. These builders keep the age, byte and reconciliation test modules
stating only the difference that matters to them.
"""

import datetime as dt
import typing as typ
from pathlib import Path

from buildlimits.outcome_archive_history import ActiveHistory
from buildlimits.outcome_archive_plan import Plan, PlanInputs, plan_run
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segment_inventory import Inventory, SegmentEntry
from buildlimits.outcome_segments import segment_name

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)

# ``None`` means the entry has no metadata at all, while an empty span keeps
# metadata whose observation times are unknown.
Span = tuple[dt.datetime | None, dt.datetime | None] | None

__all__ = [
    "NOW",
    "archived",
    "entry",
    "plan",
    "policy",
    "sealed",
]


def policy(
    *,
    max_age_days: int = 30,
    max_bytes: int = 0,
    max_records: int | None = None,
) -> RetentionPolicy:
    """Build one policy with eviction bounds chosen by the test.

    Returns
    -------
    RetentionPolicy
        The policy, with a one-byte segment and headroom.
    """
    return RetentionPolicy(
        max_age=dt.timedelta(days=max_age_days),
        max_bytes=max_bytes,
        max_records=max_records,
        headroom_bytes=1,
        segment_bytes=1,
    )


def entry(
    sequence: int,
    *,
    state: typ.Literal["sealed", "archived"] = "archived",
    logical_bytes: int = 100,
    span: Span = (None, None),
) -> SegmentEntry:
    """Build one inventory entry without touching a filesystem.

    Returns
    -------
    SegmentEntry
        An entry describing the requested state.
    """
    first, last = span if span is not None else (None, None)
    has_metadata = span is not None
    return SegmentEntry(
        sequence=sequence,
        path=Path("/state") / segment_name(sequence),
        state=state,
        physical_bytes=40,
        logical_bytes=logical_bytes,
        record_count=1 if has_metadata else None,
        first_observed_at=first,
        last_observed_at=last,
        salt_id="salt-1" if has_metadata else None,
        has_metadata=has_metadata,
    )


def archived(
    sequence: int,
    *,
    logical_bytes: int = 100,
    span: Span = (None, None),
) -> SegmentEntry:
    """Build one archived entry.

    Returns
    -------
    SegmentEntry
        The entry.
    """
    return entry(
        sequence,
        state="archived",
        logical_bytes=logical_bytes,
        span=span,
    )


def sealed(sequence: int, logical_bytes: int = 100) -> SegmentEntry:
    """Build one sealed source entry.

    Returns
    -------
    SegmentEntry
        The entry.
    """
    return entry(sequence, state="sealed", logical_bytes=logical_bytes)


def plan(
    inventory: Inventory,
    retention: RetentionPolicy,
    *,
    active: ActiveHistory | None = None,
) -> Plan:
    """Plan one run at a fixed wall time.

    The active measurement is passed whole, so a test states the size, row
    count and completeness of the read it describes without a switch for
    each. An omitted measurement is an empty, complete active file.

    Returns
    -------
    Plan
        The planner's decision.
    """
    measured = ActiveHistory() if active is None else active
    return plan_run(inventory, retention, PlanInputs(now=NOW, active=measured))
