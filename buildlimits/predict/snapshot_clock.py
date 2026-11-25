"""Refuse a snapshot whose recorded clock cannot order its history.

The recorder stamps every row of one reading with that reading's own moment,
so rows a store appended together share a time. Two rules then decide whether
the file can be ordered at all: verified label times must not decrease in
append order, and a label cannot have been available before a decision the
file already recorded. No monotonic sequence is persisted beside the times, so
the two sides of a clock step cannot be separated; a history that crosses one
is refused rather than scored.
"""

import itertools
import typing as typ

from buildlimits.predict.events import label_moment
from buildlimits.predict.timing import audit as timing_audit
from buildlimits.utc_text import utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    from buildlimits.predict.events import Event


def require_ordered_clock(path: Path, events: cabc.Sequence[Event]) -> None:
    """Refuse a history whose recorded clock ran backwards.

    The recorder stamps every row of one reading with that reading's own
    moment, so rows a store appended together share a time, and a later
    position carrying an earlier stamp means the wall clock stepped backwards.
    No monotonic sequence is stored beside the times, so the two sides of a
    step cannot be separated: an event admitted later could sort before a
    label observed earlier, and the schedule would predict with a label not yet
    observed. A history carrying admissions that crosses a step is refused
    rather than scored; one with no admissions is unaffected, because it is
    already reported as unpredicted.

    Only two verified label times are a clock question. A row the audit
    excludes is not evidence, so its stamp cannot decide the question; a
    repeated attempt carries its original's moment and would otherwise report a
    step that never happened. Rows are appended in decision order rather than
    label order, so the comparison is made over the verified times in append
    order, not over raw ``observed_at`` values.

    Raises
    ------
    ValueError
        If an admission-bearing history records an earlier verified label time
        after a later one.
    """
    if not any(event.admitted_at is not None for event in events):
        return
    usable = timing_audit(events).usable
    verified = _verified_label_times(path, events, usable)
    for earlier, later in itertools.pairwise(verified):
        if later < earlier:
            msg = (
                f"{path} records {utc_text(later)} after {utc_text(earlier)}; "
                "label-availability times cannot order a history whose clock "
                "ran backwards"
            )
            raise ValueError(msg)


def _verified_label_times(
    path: Path, events: cabc.Sequence[Event], usable: cabc.Sequence[bool]
) -> list[dt.datetime]:
    """Return the verified label times whose append order a clock can order.

    Two rules decide a step. A later verified label time must not precede an
    earlier one, because one store appends rows in the order its readings
    returned. A later verified label time must also not precede an admission
    recorded earlier in the file: that decision was taken before the row
    containing it was appended, so under a monotonic clock it is strictly
    earlier than any later row's reading. The second rule is what catches a
    step that leaves the label times themselves increasing while crossing a
    decision they must follow.

    A row the audit excludes is not evidence, so its label time cannot decide
    the question; a repeated attempt carries its original's moment and would
    otherwise report a step that never happened. Admissions are taken from
    every row that records one, because a decision time remains evidence even
    when its label is withheld.

    Returns
    -------
    list[datetime]
        The verified label times, in append order.

    Raises
    ------
    ValueError
        If a verified label time precedes an admission recorded before it.
    """
    verified: list[dt.datetime] = []
    latest_admission: dt.datetime | None = None
    for index, event in enumerate(events):
        available = label_moment(event) if usable[index] else None
        if available is not None:
            if latest_admission is not None and available < latest_admission:
                msg = (
                    f"{path} records a label available at {utc_text(available)} "
                    f"after a decision taken at {utc_text(latest_admission)}; "
                    "label-availability times cannot precede a decision the "
                    "clock already recorded"
                )
                raise ValueError(msg)
            verified.append(available)
        if event.admitted_at is not None:
            latest_admission = (
                event.admitted_at
                if latest_admission is None
                else max(latest_admission, event.admitted_at)
            )
    return verified
