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

    from buildlimits.predict.events import Event

__all__ = ["ClockOrderError", "require_ordered_clock"]


class ClockOrderError(ValueError):
    """A history whose recorded clock cannot order it.

    The decision is stated in the domain's own terms — the moments it compared
    — so a caller that knows which file it was reading can attach that fact
    where the file is opened rather than passing a path down here.

    Attributes
    ----------
    available : datetime
        The later-appended label time the rule refused.
    decision : datetime | None
        The earlier decision it preceded, when that was the failing rule.
    earlier : datetime | None
        The earlier label time it stepped back from, for the ordering rule.
    """

    def __init__(
        self,
        *,
        available: dt.datetime,
        decision: dt.datetime | None = None,
        earlier: dt.datetime | None = None,
    ) -> None:
        """State the two moments the rule compared."""
        self.available = available
        self.decision = decision
        self.earlier = earlier
        super().__init__(self._describe())

    def _describe(self) -> str:
        """Return the sentence the persistence adapter reports verbatim.

        Returns
        -------
        str
            The closed description of the disorder.
        """
        if self.decision is not None:
            return (
                f"a label available at {utc_text(self.available)} follows a "
                f"decision taken at {utc_text(self.decision)}; "
                "label-availability times cannot precede a decision the clock "
                "already recorded"
            )
        earlier = typ.cast("dt.datetime", self.earlier)
        return (
            f"{utc_text(self.available)} follows {utc_text(earlier)}; "
            "label-availability times cannot order a history whose clock ran "
            "backwards"
        )

    def at(self, path: object) -> ClockOrderError:
        """Return an equal disorder whose message names ``path``.

        Returns
        -------
        ClockOrderError
            The same moments, with the reader's location stated.
        """
        located = ClockOrderError(
            available=self.available,
            decision=self.decision,
            earlier=self.earlier,
        )
        located.args = (f"{path} records {located}",)
        return located


def _require_after_decision(
    available: dt.datetime, decision: dt.datetime | None
) -> None:
    """Refuse a label time that precedes a decision the file already holds.

    Raises
    ------
    ClockOrderError
        If a later-appended label time is earlier than an earlier decision.
    """
    if decision is None or available >= decision:
        return
    raise ClockOrderError(available=available, decision=decision)


def _later(
    latest: dt.datetime | None, candidate: dt.datetime | None
) -> dt.datetime | None:
    """Return the later of two optional moments.

    Returns
    -------
    datetime | None
        The later moment, or whichever is present.
    """
    if latest is None:
        return candidate
    if candidate is None:
        return latest
    return max(latest, candidate)


def require_ordered_clock(events: cabc.Sequence[Event]) -> None:
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
    ClockOrderError
        If an admission-bearing history records an earlier verified label time
        after a later one, or a label before an earlier decision.
    """
    if not any(event.admitted_at is not None for event in events):
        return
    usable = timing_audit(events).usable
    verified = _verified_label_times(events, usable)
    for earlier, later in itertools.pairwise(verified):
        if later < earlier:
            raise ClockOrderError(available=later, earlier=earlier)


def _verified_label_times(
    events: cabc.Sequence[Event], usable: cabc.Sequence[bool]
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
        The verified label times, in append order. A label time that precedes
        an earlier decision is refused by :func:`_require_after_decision`.
    """
    verified: list[dt.datetime] = []
    latest_admission: dt.datetime | None = None
    for index, event in enumerate(events):
        available = label_moment(event) if usable[index] else None
        if available is not None:
            _require_after_decision(available, latest_admission)
            verified.append(available)
        latest_admission = _later(latest_admission, event.admitted_at)
    return verified
