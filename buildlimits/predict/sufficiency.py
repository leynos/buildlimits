"""Decide whether the replayed history supports a held-out comparison.

The decision is kept apart from the evaluation that reports it because it is
read by two callers — the evaluation's status field and its plain-language
verdict — and both must reach the same conclusion from the same counts. The
counts travel together for the same reason: a label shortage, a decision
shortage and an availability shortage are told apart by comparing them, so
passing them separately invites a caller to pair one gate's denominator with
another gate's numerator.

The minimum-label gate counts independent labelled rows, so repeated appends
cannot clear it. A history that clears it can still hold no event with a usable
admission time, and then nothing could be predicted at all. A source that
records the decision but never the moment a label became available is reported
separately, because the decision time alone cannot prove the label was knowable
before a later prediction. When predictions do exist, too few paired rows, or
rows forming a single block, are likewise reported separately.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.bootstrap import Interval
    from buildlimits.predict.config import Config

__all__ = [
    "STATUS_INSUFFICIENT",
    "STATUS_INSUFFICIENT_ADMISSION",
    "STATUS_INSUFFICIENT_AVAILABILITY",
    "STATUS_INSUFFICIENT_HELDOUT",
    "STATUS_OK",
    "EvidenceCounts",
    "evidence_status",
    "has_evidence",
]

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient data"
STATUS_INSUFFICIENT_ADMISSION = "insufficient admission times"
STATUS_INSUFFICIENT_AVAILABILITY = "insufficient label-availability times"
STATUS_INSUFFICIENT_HELDOUT = "insufficient held-out evidence"
# One block resamples to itself, so its interval has no width to judge.
MINIMUM_BLOCKS = 2


@dc.dataclass(frozen=True, slots=True)
class EvidenceCounts:
    """The population counts every status and verdict sentence is decided from.

    They travel together because each gate is read against the others: a label
    shortage, a decision shortage and an availability shortage are told apart
    by comparing these numbers, so passing them separately invites a caller to
    pair one gate's denominator with another gate's numerator.
    """

    labelled: int
    usable_labelled: int
    admission_recorded: int
    unverified_label_time: int
    predictable: int
    unavailable_availability: int


def _label_shortage(counts: EvidenceCounts, config: Config) -> str | None:
    """Return the status a failed independence gate implies.

    The independence gate is what failed when this reports anything. When the
    recorded labels are plentiful enough that another gate is plainly the real
    shortage, the status names that gate instead; otherwise the history is
    short of usable labels.

    Returns
    -------
    str | None
        The status to report, or ``None`` when the gate is satisfied.
    """
    if counts.usable_labelled >= config.minimum_labelled_events:
        return None
    plenty = counts.labelled >= config.minimum_labelled_events
    if plenty and counts.admission_recorded == 0:
        return STATUS_INSUFFICIENT_ADMISSION
    if plenty and counts.unverified_label_time == counts.labelled:
        return STATUS_INSUFFICIENT_AVAILABILITY
    return STATUS_INSUFFICIENT


def has_evidence(interval: Interval | None, config: Config) -> bool:
    """Return whether an interval carries enough held-out evidence to judge.

    Returns
    -------
    bool
        Whether the interval was paired over enough rows, in at least two
        blocks, for its width to mean anything.
    """
    return (
        interval is not None
        and interval.rows >= config.minimum_scored_events
        and interval.blocks >= MINIMUM_BLOCKS
    )


def evidence_status(
    counts: EvidenceCounts,
    against_base: cabc.Mapping[str, Interval | None],
    config: Config,
) -> str:
    """Return the status the evidence supports.

    Returns
    -------
    str
        ``ok``, ``insufficient data``, ``insufficient admission times``,
        ``insufficient label-availability times`` or ``insufficient held-out
        evidence``.
    """
    shortage = _label_shortage(counts, config)
    if shortage is not None:
        return shortage
    if counts.predictable == 0:
        if counts.unavailable_availability:
            return STATUS_INSUFFICIENT_AVAILABILITY
        return STATUS_INSUFFICIENT_ADMISSION
    if not any(has_evidence(interval, config) for interval in against_base.values()):
        return STATUS_INSUFFICIENT_HELDOUT
    return STATUS_OK
