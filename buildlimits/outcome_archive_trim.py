"""Prefix-trim policy and the measurement a byte allowance composes with.

A rewrite can drop leading rows by count, by observation time, or down to a
byte allowance. The first two rules apply to the whole document, while the
allowance bounds the document that survives them, so their effect has to be
measured before the suffix is chosen. Every rule is evaluated one row at a
time, so the measurement keeps one row in memory whatever the source size.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import datetime as dt

__all__ = ["Trim", "fits_allowance", "policy_drops"]


@dc.dataclass(frozen=True, slots=True)
class Trim:
    """A prefix trim applied while a segment is rewritten.

    Attributes
    ----------
    before : datetime | None
        Drop rows observed strictly before this time.
    records : int
        Drop this many leading rows regardless of time.
    allowed_bytes : int | None
        Keep at most this many logical bytes, dropping leading rows. The
        header is always written and counts against the allowance, because
        the plan compares this bound with ``ArchiveFacts.logical_bytes``.
    source_bytes : int
        Original logical size, needed to honour ``allowed_bytes`` exactly.
    policy_bytes : int
        Bytes of record rows that ``records`` and ``before`` drop over the
        whole source, as measured by ``policy_dropped_bytes``. Those rows stay
        out of the written document wherever they appear, so the byte
        allowance must be compared with what survives them. Zero means the
        measurement was not supplied, which makes a composed trim discard
        eligible rows rather than exceed the allowance.
    """

    before: dt.datetime | None = None
    records: int = 0
    allowed_bytes: int | None = None
    source_bytes: int = 0
    policy_bytes: int = 0

    @property
    def is_empty(self) -> bool:
        """Whether the trim would keep every row.

        Returns
        -------
        bool
            Whether all trim fields are inert.
        """
        return self.before is None and self.records == 0 and self.allowed_bytes is None


def policy_drops(observed_at: dt.datetime, trim: Trim, *, seen: int) -> bool:
    """Whether the count or age rule drops one record.

    Parameters
    ----------
    observed_at : datetime
        The candidate row's observation time.
    trim : Trim
        The prefix policy.
    seen : int
        Records already decided, used by the explicit count trim.

    Returns
    -------
    bool
        Whether a non-byte rule drops the row.
    """
    if seen < trim.records:
        return True
    return trim.before is not None and observed_at < trim.before


def fits_allowance(trim: Trim, *, dropped: int) -> bool:
    """Whether keeping this row leaves a document within the byte allowance.

    The header is always written, so what a trim frees is exactly the rows it
    drops. Keeping this row and every later one writes everything except the
    policy-dropped rows and the byte-dropped prefix, and that whole document
    must fit the allowance. ``planned_size`` relies on this bound, so counting
    only the later rows would overstate the space the plan expects to free.

    Parameters
    ----------
    trim : Trim
        The prefix policy.
    dropped : int
        Bytes of the records the byte allowance has already dropped.

    Returns
    -------
    bool
        Whether the resulting document fits.
    """
    if trim.allowed_bytes is None:
        return True
    retained = trim.source_bytes - trim.policy_bytes - dropped
    return retained <= trim.allowed_bytes
