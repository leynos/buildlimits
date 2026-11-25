"""One event per recorded compilation, in the order the store wrote it.

Events keep the store's append order, so a backward clock step can order
timestamps against recording order without moving a record into another
window. A prediction is made at the event's admission moment, and only a label
whose observation time is strictly earlier may inform it; that ordering lives
in :mod:`buildlimits.predict.history`. An unknown outcome carries no usable
label there, so it neither counts as load nor sets an age.
"""

import dataclasses as dc
import datetime as dt
import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import Origin

type Label = typ.Literal["hit", "miss", "unknown"]
type ScoredLabel = typ.Literal["hit", "miss"]

LABELS: tuple[Label, ...] = ("hit", "miss", "unknown")
ORIGINS: tuple[Origin, ...] = ("registry", "git", "path", "unknown")
_SCORED_LABELS: frozenset[Label] = frozenset({"hit", "miss"})
EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@dc.dataclass(frozen=True, slots=True)
class Event:
    """One compilation's identity, label and when that label became known.

    Attributes
    ----------
    identity : str
        Salted, hashed compilation identity; no crate name or path is stored.
    origin : Origin
        The crate's origin class, which is known before admission.
    admitted_at : datetime | None
        When the compilation was admitted, if the source recorded it.
    observed_at : datetime
        When the label became known, as the source recorded it.
    label : Label
        ``hit`` or ``miss`` when attributed, otherwise ``unknown``.
    runtime_seconds : float
        The recorded compilation runtime, used only as a feature.
    label_observed_verified : bool
        Whether ``observed_at`` is proven to be the moment the label became
        known. It defaults to ``False`` so a value built without deciding is
        never treated as proof: a recorded time can be only a lower bound, and
        an unverified label is neither released nor scored. A source that can
        prove the time, such as a known-property synthetic stream, states it.
    """

    identity: str
    origin: Origin
    admitted_at: dt.datetime | None
    observed_at: dt.datetime
    label: Label
    runtime_seconds: float
    label_observed_verified: bool = dc.field(kw_only=True, default=False)


@dc.dataclass(frozen=True, slots=True)
class Features:
    """What a predictor may read before one event's label is known.

    Attributes
    ----------
    identity : str
        The hashed compilation identity.
    origin : Origin
        The crate's origin class.
    observed_at : datetime
        When the label became known.
    age_seconds : float | None
        Seconds from this identity's previous eligible label to the moment the
        row is measured, or None when no earlier label was known then.
    load : int
        Eligible labels inside the trailing load window that had arrived
        strictly before the measurement moment. An unknown outcome is never
        counted.
    predicted_at : datetime | None
        The admission moment the row is measured at, or None when the source
        cannot support a prediction. A row with no moment is never predicted.
    """

    identity: str
    origin: Origin
    observed_at: dt.datetime
    age_seconds: float | None
    load: int
    predicted_at: dt.datetime | None = None


def admission_moment(event: Event) -> dt.datetime | None:
    """Return the moment at which the event could have been predicted.

    A prediction needs an admission time, because that is when the decision is
    taken. A source that records none cannot support one: the label time is not
    a substitute, since assuming the label was already known then would be
    exactly the leakage the harness must prevent. An admission recorded after
    its own label describes a decision taken once the outcome was known, so it
    cannot support a prediction either. Both cases are reported as unavailable
    rather than repaired by guessing.

    Parameters
    ----------
    event : Event
        The recorded event.

    Returns
    -------
    datetime | None
        The admission time, or None when no usable one was recorded.

    Examples
    --------
    >>> admission_moment(Event("a" * 64, "git", None, EPOCH, "hit", 0.1)) is None
    True
    >>> admission_moment(
    ...     Event("a" * 64, "git", EPOCH, EPOCH, "hit", 0.1)
    ... ) == EPOCH
    True
    """
    admitted = event.admitted_at
    if admitted is None or admitted > event.observed_at:
        return None
    return admitted


def scored_label(event: Event) -> ScoredLabel | None:
    """Return the scorable label, or None when no trustworthy one exists.

    A ``hit`` or ``miss`` whose observation time is unverified is not scorable.
    Its recorded time may precede the moment the label was really known, so
    releasing it could inform a prediction admitted before that moment; the
    label is therefore withheld rather than used as ground truth. This is the
    single place that decision is made, so training, scoring and the phase
    schedule cannot disagree about which labels are usable.

    Parameters
    ----------
    event : Event
        The event whose outcome is inspected.

    Returns
    -------
    ScoredLabel | None
        ``hit`` or ``miss`` for an attributed event with a verified time,
        otherwise None.

    Examples
    --------
    >>> from buildlimits.predict.events import EPOCH
    >>> scored_label(
    ...     Event(
    ...         "a" * 64,
    ...         "git",
    ...         None,
    ...         EPOCH,
    ...         "miss",
    ...         0.1,
    ...         label_observed_verified=True,
    ...     )
    ... )
    'miss'
    >>> scored_label(Event("a" * 64, "git", None, EPOCH, "miss", 0.1)) is None
    True
    """
    label = event.label
    if label in _SCORED_LABELS and event.label_observed_verified:
        return typ.cast("ScoredLabel", label)
    return None


def unverified_label(event: Event) -> ScoredLabel | None:
    """Return a hit or miss whose recorded observation time is unverified.

    These are the labels a source recorded but could not prove were known at
    the time it stamped. They are counted separately from ``unknown`` outcomes
    so the report can say how much evidence it withheld and why.

    Parameters
    ----------
    event : Event
        The event whose provenance is inspected.

    Returns
    -------
    ScoredLabel | None
        ``hit`` or ``miss`` when the time is unverified, otherwise None.

    Examples
    --------
    >>> from buildlimits.predict.events import EPOCH
    >>> unverified_label(Event("a" * 64, "git", None, EPOCH, "hit", 0.1))
    'hit'
    >>> unverified_label(
    ...     Event(
    ...         "a" * 64,
    ...         "git",
    ...         None,
    ...         EPOCH,
    ...         "hit",
    ...         0.1,
    ...         label_observed_verified=True,
    ...     )
    ... ) is None
    True
    """
    label = event.label
    if label in _SCORED_LABELS and not event.label_observed_verified:
        return typ.cast("ScoredLabel", label)
    return None
