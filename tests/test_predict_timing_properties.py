"""Property tests for the timing, correlation and phase contracts.

Each test states one invariant that must hold for every input the strategy can
produce, rather than for a chosen example: a token is accepted exactly when it
is non-empty, bounded and drawn from the accepted alphabet; chronology is
refused exactly when a later verified label crosses an earlier moment; a
repeated attempt is a duplicate exactly when every fingerprint field agrees and
both digests are present; and a phase boundary never splits one label batch.

The examples that pin a specific case stay in the suites beside their subject.
These tests audit the rules those examples rely on.
"""

import dataclasses as dc
import datetime as dt
import itertools

from hypothesis import given, settings
from hypothesis import strategies as st
from predict_test_helpers import hand_event
from predict_timing_helpers import event

from buildlimits.outcome_record_format import FORMAT_VERSION
from buildlimits.outcome_record_timing import (
    MAX_ATTEMPT_CHARS,
    MAX_GENERATION_CHARS,
    bounded_token,
    valid_fields,
)
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.history import label_batches
from buildlimits.predict.phases import phases
from buildlimits.predict.snapshot_clock import ClockOrderError, require_ordered_clock
from buildlimits.predict.timing import audit

PROPERTY_SETTINGS = settings(max_examples=60, deadline=None)

_ACCEPTED = "abcdefghijklmnopqrstuvwxyz0123456789._-"
_HEADER = (
    f'{{"format":"buildlimits-cache-outcomes","version":{FORMAT_VERSION},'
    '"salt_id":"s","written_at":"2026-01-01T00:00:00Z"}'
)


@st.composite
def _tokens(draw: st.DrawFn, maximum: int) -> str:
    """Return a string of any length around ``maximum`` from any alphabet.

    Returns
    -------
    str
        A candidate token, accepted or not.
    """
    length = draw(st.integers(min_value=0, max_value=maximum + 2))
    return draw(st.text(alphabet=_ACCEPTED + " /\\", min_size=length, max_size=length))


def _accepts(value: str, maximum: int) -> bool:
    """Return the contract's own verdict for one candidate token.

    Returns
    -------
    bool
        Whether a non-empty, bounded, path-free token was supplied.
    """
    return 0 < len(value) <= maximum and all(
        character in _ACCEPTED for character in value
    )


@PROPERTY_SETTINGS
@given(value=_tokens(MAX_ATTEMPT_CHARS))
def test_a_token_is_accepted_exactly_when_its_contract_holds(value: str) -> None:
    """`bounded_token` accepts exactly the non-empty, bounded, safe tokens."""
    assert bounded_token(value, MAX_ATTEMPT_CHARS) is _accepts(
        value, MAX_ATTEMPT_CHARS
    ), "the predicate and the contract agree for every candidate"


@PROPERTY_SETTINGS
@given(
    attempt=st.one_of(
        st.none(),
        _tokens(MAX_ATTEMPT_CHARS).filter(
            lambda token: _accepts(token, MAX_ATTEMPT_CHARS)
        ),
    ),
    generation=st.one_of(
        st.none(),
        _tokens(MAX_GENERATION_CHARS).filter(
            lambda token: _accepts(token, MAX_GENERATION_CHARS)
        ),
    ),
)
def test_the_field_validator_accepts_exactly_the_complete_pairs(
    attempt: str | None, generation: str | None
) -> None:
    """The decoder's field validator accepts exactly the complete pairs."""
    document = {
        "kind": "outcome",
        "attempt": attempt,
        "supervisor_generation": generation,
    }

    accepted = valid_fields(document)

    assert accepted is ((attempt is None) == (generation is None)), (
        "a pair is accepted exactly when both halves are present or absent"
    )


@PROPERTY_SETTINGS
@given(
    labels=st.lists(st.integers(min_value=0, max_value=60), min_size=2, max_size=8),
    extra=st.integers(min_value=0, max_value=60),
)
def test_chronology_is_refused_exactly_at_a_crossing(
    labels: list[int], extra: int
) -> None:
    """The rule refuses a history exactly when a later label crosses a moment.

    Every admission is placed at or before its own label, so every row is
    usable and the rule sees the whole stream. The two failures the rule
    models are a decreasing verified label time and a label that precedes an
    admission recorded earlier in the file; nothing else may be refused.
    """
    admitted = [min(extra, moment) for moment in labels]
    events = tuple(
        event(index, moments=(admitted[index], float(moment)))
        for index, moment in enumerate(labels)
    )
    decreasing = any(later < earlier for earlier, later in itertools.pairwise(labels))
    crosses_decision = any(
        labels[index] < max(admitted[:index]) for index in range(1, len(labels))
    )

    try:
        require_ordered_clock(events)
    except ClockOrderError:
        refused = True
    else:
        refused = False

    assert refused is (decreasing or crosses_decision), (
        "the history is refused exactly when a later label crosses a moment"
    )


_FINGERPRINT_FIELDS = (
    "identity",
    "origin",
    "label",
    "admitted_at",
    "completed_at",
    "label_observed_verified",
    "observed_at",
    "runtime_seconds",
    "generation_digest",
    "record_evidence",
)


def _agrees(original: Event, repeat: Event) -> bool:
    """Return whether two rows of one attempt record identical evidence.

    Returns
    -------
    bool
        Whether every fingerprint field agrees and both evidence digests are
        present, which is what makes a repeated append an idempotent retry.
    """
    if original.record_evidence is None or repeat.record_evidence is None:
        return False
    return all(
        getattr(original, name) == getattr(repeat, name) for name in _FINGERPRINT_FIELDS
    )


@PROPERTY_SETTINGS
@given(
    first=st.integers(min_value=0, max_value=30),
    change=st.sampled_from((
        "none",
        "runtime",
        "completed",
        "observed",
        "label",
        "evidence_absent",
    )),
)
def test_a_repeated_attempt_is_a_duplicate_only_on_complete_agreement(
    first: int, change: str
) -> None:
    """Only equal fingerprints with both digests present are a duplicate."""
    original = dc.replace(
        hand_event(
            0,
            moments=(EPOCH, EPOCH + dt.timedelta(seconds=first)),
            label="hit",
        ),
        attempt="same",
        supervisor_generation="gen",
    )
    repeat = {
        "none": original,
        "runtime": dc.replace(original, runtime_seconds=2.5),
        "completed": dc.replace(
            original, completed_at=EPOCH + dt.timedelta(seconds=first + 1)
        ),
        "observed": dc.replace(
            original, observed_at=EPOCH + dt.timedelta(seconds=first + 2)
        ),
        "label": dc.replace(original, label="miss"),
        "evidence_absent": dc.replace(original, record_evidence=None),
    }[change]

    verdict = audit((original, repeat))

    assert verdict.duplicates == (1 if _agrees(original, repeat) else 0), (
        "a retry is a duplicate only when the recorded evidence agrees"
    )
    assert verdict.conflicts == (0 if _agrees(original, repeat) else 2), (
        "and every disagreement or absent digest is a conflict"
    )


@PROPERTY_SETTINGS
@given(
    count=st.integers(min_value=2, max_value=12),
    fraction=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_a_phase_boundary_never_splits_a_label_batch(
    count: int, fraction: float
) -> None:
    """Every equal-time batch lands wholly on one side of the boundary."""
    events = tuple(
        dc.replace(
            hand_event(
                index,
                moments=(EPOCH, EPOCH + dt.timedelta(seconds=index // 3)),
                label="hit",
            ),
            attempt=f"a{index}",
        )
        for index in range(count)
    )
    timing = audit(events)
    assigned = phases(events, timing, train_fraction=fraction)

    batches = label_batches(events)
    for batch in batches:
        sides = {assigned[index] for index in batch}
        assert len(sides) == 1, "one reading never straddles the boundary"

    assert sum(1 for phase in assigned if phase == "train") <= int(
        count * fraction
    ) + max(len(batch) for batch in batches), (
        "the tuning share exceeds its target by at most one batch"
    )
