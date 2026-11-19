"""The covered span and sample limits that every replay verdict states."""

import dataclasses as dc
import typing as typ

from buildlimits.utc_text import utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

QUIET_WINDOW_CAVEAT = (
    "labels come from solo and unanimous windows, so this sample may not "
    "represent busy, mixed traffic"
)
CHILD_SIGHTING_CAVEAT = (
    "child sightings are sampled every 0.25 s, so a full-emit child that lives "
    "less than that can be missed between samples, which biases the sighting "
    "clue towards long misses"
)
H2_LIMITS = (QUIET_WINDOW_CAVEAT, CHILD_SIGHTING_CAVEAT)
H3_LIMITS = (QUIET_WINDOW_CAVEAT,)


class ObservedRecord(typ.Protocol):
    """A store value that records when it was observed."""

    @property
    def observed_at(self) -> dt.datetime:
        """The UTC time at which the record was observed."""
        ...


@dc.dataclass(frozen=True, slots=True)
class Coverage:
    """The earliest and latest observation time a report read.

    The store keeps at most 10,000 records (R8), so this span, rather than a
    retention period, says how much history a verdict rests on.
    """

    first: dt.datetime | None = None
    last: dt.datetime | None = None

    @classmethod
    def over(cls, times: cabc.Iterable[dt.datetime]) -> Coverage:
        """Return the span covered by the supplied observation times.

        Parameters
        ----------
        times : cabc.Iterable[dt.datetime]
            Observation times of the records a report read.

        Returns
        -------
        Coverage
            The earliest and latest times, or both None for an empty read.
        """
        values = tuple(times)
        if not values:
            return cls()
        return cls(min(values), max(values))

    def as_dict(self) -> dict[str, str | None]:
        """Return the span in the store's canonical UTC ISO form.

        Returns
        -------
        dict[str, str | None]
            The ``from`` and ``until`` bounds, or None when nothing was read.
        """
        return {
            "from": None if self.first is None else utc_text(self.first),
            "until": None if self.last is None else utc_text(self.last),
        }


def coverage_of(records: cabc.Iterable[ObservedRecord]) -> Coverage:
    """Return the span covered by the observation time of each record.

    Parameters
    ----------
    records : cabc.Iterable[ObservedRecord]
        The store values a report read, in store order.

    Returns
    -------
    Coverage
        The earliest and latest observation times.
    """
    return Coverage.over(record.observed_at for record in records)


__all__ = [
    "CHILD_SIGHTING_CAVEAT",
    "H2_LIMITS",
    "H3_LIMITS",
    "QUIET_WINDOW_CAVEAT",
    "Coverage",
    "ObservedRecord",
    "coverage_of",
]
