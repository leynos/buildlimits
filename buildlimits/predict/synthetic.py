"""Known-property event streams for the benchmark's own tests.

These streams never feed a real report. Each one is built to make a single
claim checkable: a stable hit rate, perfect persistence, a mid-stream drift,
or a label that arrives long after its compilation started.

Every stream records an admission time, because a prediction is made at
admission and a stream without one supports no prediction at all. Unless a
stream is testing label delay, an event is admitted :data:`LABEL_DELAY` before
its label arrives, so each label is released before the next event is admitted.
"""

import datetime as dt
import hashlib
import random

from buildlimits.predict.events import EPOCH, Event, Label

STEP = dt.timedelta(seconds=30)
LABEL_DELAY = dt.timedelta(seconds=5)
_ORIGINS = ("registry", "git", "path")
_PERSISTENCE_RATE = 0.5
# An even chance of a hit, so neither label can dominate a synthetic stream.
_BALANCED_RATE = 0.5


def identity(index: int) -> str:
    """Return a stable 64-character identity for one synthetic crate.

    Returns
    -------
    str
        A SHA-256 digest of the index.
    """
    return hashlib.sha256(f"synthetic-{index}".encode()).hexdigest()


def _next(seed: int) -> random.Random:
    """Seed one reproducible generator.

    Returns
    -------
    random.Random
        The seeded generator.
    """
    # ruff: ignore[suspicious-non-cryptographic-random-usage] - synthetic streams
    # need reproducibility, not unpredictability.
    return random.Random(seed)


def _event(index: int, label: Label, *, admitted_at: dt.datetime | None) -> Event:
    """Build one event at its stream position.

    Returns
    -------
    Event
        The synthetic event.
    """
    return Event(
        identity=identity(index % 20),
        origin=_ORIGINS[index % len(_ORIGINS)],
        admitted_at=admitted_at,
        observed_at=EPOCH + index * STEP,
        label=label,
        runtime_seconds=1.0 + (index % 7) / 10,
    )


def _causal_event(index: int, label: Label) -> Event:
    """Build one event admitted shortly before its own label.

    Returns
    -------
    Event
        The synthetic event, predictable at a time after every earlier label.
    """
    return _event(index, label, admitted_at=EPOCH + index * STEP - LABEL_DELAY)


def stationary(*, count: int, rate: float, seed: int) -> tuple[Event, ...]:
    """Return a stream whose hit rate is the same before and after the split.

    Returns
    -------
    tuple[Event, ...]
        ``count`` events in stream order.
    """
    generator = _next(seed)
    return tuple(
        _causal_event(index, "hit" if generator.random() < rate else "miss")
        for index in range(count)
    )


def persistent(*, count: int, seed: int) -> tuple[Event, ...]:
    """Return a stream where each identity always repeats its own outcome.

    Returns
    -------
    tuple[Event, ...]
        ``count`` events; the first sighting of each identity is random.
    """
    generator = _next(seed)
    fixed: dict[str, Label] = {
        identity(index): ("hit" if generator.random() < _PERSISTENCE_RATE else "miss")
        for index in range(20)
    }
    return tuple(
        _causal_event(index, fixed[identity(index % 20)]) for index in range(count)
    )


def drift(
    *, count: int, first_rate: float, second_rate: float, seed: int
) -> tuple[Event, ...]:
    """Return a stream whose hit rate changes halfway through.

    Returns
    -------
    tuple[Event, ...]
        ``count`` events; the first half uses ``first_rate``.
    """
    generator = _next(seed)
    half = count // 2
    return tuple(
        _causal_event(
            index,
            "hit"
            if generator.random() < (first_rate if index < half else second_rate)
            else "miss",
        )
        for index in range(count)
    )


def delayed(*, count: int, delay_steps: int) -> tuple[Event, ...]:
    """Return a stream whose labels arrive well after each admission.

    Each event is admitted between one and five ``delay_steps`` before its label
    arrives, and the delay varies along the stream. Admission order therefore
    differs from label order: a compilation started early can finish after one
    started later, which is the case a walk-forward harness must respect.

    Returns
    -------
    tuple[Event, ...]
        ``count`` events ordered by label time, with staggered admissions.
    """
    return tuple(
        _event(
            index,
            "hit" if index % 2 == 0 else "miss",
            admitted_at=EPOCH
            + index * STEP
            - (1 + (index * 7) % 5) * delay_steps * STEP,
        )
        for index in range(count)
    )


def with_unknowns(*, count: int) -> tuple[Event, ...]:
    """Return a stream where every third outcome is unattributable.

    Returns
    -------
    tuple[Event, ...]
        ``count`` events, one third of them ``unknown``.
    """
    return tuple(
        _causal_event(
            index, "unknown" if index % 3 == 0 else ("hit" if index % 2 else "miss")
        )
        for index in range(count)
    )


def persistence_shift(*, count: int, seed: int) -> tuple[Event, ...]:
    """Return a stream that stops being random halfway through.

    During the first half every outcome is drawn afresh, so repeating an
    identity's last outcome is a poor guide. From the halfway point each
    identity keeps one outcome for the rest of the stream, so the last outcome
    becomes the better guide while the global hit rate stays near one half.
    An adaptive ensemble should move its weight towards last-outcome after the
    shift and away from it before.

    Returns
    -------
    tuple[Event, ...]
        ``count`` events; the first half is random, the second persistent.
    """
    generator = _next(seed)
    half = count // 2
    fixed: dict[str, Label] = {
        identity(index): ("hit" if generator.random() < _BALANCED_RATE else "miss")
        for index in range(20)
    }
    return tuple(
        _causal_event(
            index,
            ("hit" if generator.random() < _BALANCED_RATE else "miss")
            if index < half
            else fixed[identity(index % 20)],
        )
        for index in range(count)
    )
