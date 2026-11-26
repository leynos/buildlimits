"""Causal replay contracts for verified decision and availability times.

Every test uses injected clocks. The scenarios are the issue's regressions:

- A completes at 2, B decides at 3, and A's attribution is accepted at 5.
  B cannot learn A's label; a decision after 5 can; a decision at exactly 5
  still obeys the strict-before rule.
- Two invocations with one cache key that finish out of launch order keep their
  own attempt identities and availability times.
- Delayed writes, duplicate or conflicting appends, restart boundaries and a
  clock step never move a label into an earlier prediction's history.

The store, snapshot and report round trips are in
``test_predict_timing_roundtrip.py``.
"""

import dataclasses as dc
import datetime as dt
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from predict_test_helpers import hand_event
from predict_timing_helpers import at, config, event, reviewer_stream

from buildlimits.predict.events import EPOCH, Event, label_moment
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.history import (
    label_batches,
    prediction_moments,
    released_labels,
)
from buildlimits.predict.timing import audit, exclusions, scored


def test_a_decision_before_a_label_became_available_cannot_learn_it() -> None:
    """B decides at 3, A is accepted at 5, so B's history is empty."""
    stream = reviewer_stream()

    releases = released_labels(stream)
    assert [index for _moment, index in releases] == [0, 1, 2, 3], (
        "every verified label is releasable in acceptance order"
    )
    walk = walk_forward(stream, config())
    rows = [typ.cast("typ.Any", row) for row in walk.rows]

    assert rows[1].load == 0, "B decides before A's reading was accepted"
    assert rows[1].age_seconds is None, "no earlier label sets an age for B"
    assert rows[2].load == 1, "C decides after A alone was accepted"
    assert rows[3].load == 0, (
        "a decision at exactly the availability instant sees nothing accepted "
        "at that instant: A is not strictly earlier and D is its own label"
    )


def test_a_decision_after_the_acceptance_learns_the_label() -> None:
    """The same label is available to a decision strictly later than 5."""
    later = event(9, moments=(5.5, 30.0), label="miss")
    walk = walk_forward((*reviewer_stream(), later), config())

    assert typ.cast("typ.Any", walk.rows[4]).load == 1, (
        "only A was accepted before the half-second-later decision"
    )
    assert typ.cast("typ.Any", walk.rows[4]).age_seconds is None, (
        "the identity has no earlier label of its own"
    )


def test_a_label_is_never_released_at_its_completion_time() -> None:
    """Completion is retained for provenance and never used as availability."""
    slow_accept = dc.replace(event(0, moments=(1.0, 5.0)), completed_at=at(2.0))
    between = event(1, moments=(3.0, 9.0), label="miss")

    walk = walk_forward((slow_accept, between), config())

    assert typ.cast("typ.Any", walk.rows[1]).load == 0, (
        "the label completed at 2 but was accepted at 5, so B cannot read it"
    )
    assert slow_accept.completed_at == at(2.0), "the completion time is retained"


def _attempt(
    index: int,
    *,
    moments: tuple[dt.datetime | None, dt.datetime],
    label: str,
    attempt: str,
) -> Event:
    """Return one hand-built event naming a chosen attempt token.

    The token is part of the scenario, so it is stated here rather than
    derived, and two events built this way never collapse into a duplicate.

    Returns
    -------
    Event
        The verified event carrying ``attempt``.
    """
    return dc.replace(
        hand_event(index, moments=moments, label=typ.cast("typ.Any", label)),
        attempt=attempt,
    )


def test_concurrent_identical_invocations_keep_their_own_timing() -> None:
    """Two attempts for one identity never share a completion or availability."""
    first = _attempt(0, moments=(at(1.0), at(9.0)), label="hit", attempt="attempt-a")
    second = _attempt(0, moments=(at(2.0), at(4.0)), label="miss", attempt="attempt-b")

    walk = walk_forward((first, second), config())
    verdict = audit((first, second))

    assert first.identity == second.identity, "both invocations share one key"
    assert label_moment(first) == at(9.0), "the slow attempt keeps its own"
    assert label_moment(second) == at(4.0), "the fast attempt keeps its own"
    assert verdict.reasons == (None, None), "neither row is a duplicate"
    assert typ.cast("typ.Any", walk.rows[1]).load == 0, (
        "the later decision precedes the slow attempt's acceptance"
    )


@given(
    finishes=st.lists(
        st.integers(min_value=0, max_value=40), min_size=2, max_size=6, unique=True
    )
)
@settings(max_examples=40, deadline=None)
def test_availability_never_depends_on_reversed_launch_order(
    finishes: list[int],
) -> None:
    """Any permutation of completion order yields each row's own acceptance.

    Each event is admitted before every generated acceptance moment, so every
    ordering is physically possible and the only question is whether the rows
    keep their own times instead of inheriting a neighbour's.
    """
    ordered = sorted(finishes)
    events = tuple(
        event(
            position,
            moments=(-1.0, float(finish)),
            attempt=f"attempt-{position}",
        )
        for position, finish in enumerate(finishes)
    )

    verdict = audit(events)

    assert verdict.reasons == (None,) * len(events), "distinct attempts never collide"
    assert [label_moment(record) for record in events] == [
        at(float(finish)) for finish in finishes
    ], "each row's acceptance is its own, whatever order the launches finished"
    assert sorted(
        typ.cast("dt.datetime", label_moment(record)) for record in events
    ) == [at(float(finish)) for finish in ordered], "the set of moments is unchanged"


def test_an_exact_duplicate_append_is_an_idempotent_duplicate() -> None:
    """A retried append with identical evidence does not create a second label."""
    original = event(0, moments=(1.0, 5.0))
    retried = dc.replace(original, observed_at=original.observed_at)

    verdict = audit((original, retried))

    assert verdict.duplicates == 1, "the retry is counted as a duplicate"
    assert verdict.conflicts == 0, "identical evidence is not a conflict"
    assert verdict.reasons == (None, "duplicate_attempt"), (
        "the first copy is scored and the retry is excluded"
    )
    releases = released_labels((original, retried))
    assert [index for _moment, index in releases] == [0], (
        "a duplicate can never double a predictor's history"
    )


def test_rows_that_disagree_about_one_attempt_are_unusable_together() -> None:
    """Nothing says which copy of a contradictory attempt the observer accepted."""
    first = event(0, moments=(1.0, 5.0), label="hit", attempt="same")
    second = event(0, moments=(1.0, 5.0), label="miss", attempt="same")

    verdict = audit((first, second))

    assert verdict.conflicts == 2, "both copies are conflicts"
    assert verdict.reasons == ("conflicting_attempt", "conflicting_attempt"), (
        "a contradictory pair is excluded rather than resolved"
    )
    assert released_labels((first, second)) == [], "neither releases a label"
    assert not walk_forward((first, second), config()).moments, (
        "neither supplies a prediction moment either"
    )


def test_a_restart_generation_separates_reused_group_identifiers() -> None:
    """The same attempt token in a new supervisor is not the same attempt."""
    before = event(0, moments=(1.0, 5.0), attempt="g1")
    after = dc.replace(
        event(0, moments=(1.0, 5.0), attempt="g1"),
        supervisor_generation="generation-b",
    )

    verdict = audit((before, after))

    assert verdict.reasons == (None, None), "two generations never join"
    assert before.attempt == after.attempt, "the token alone is reused"


def test_an_unverified_row_is_excluded_rather_than_backdated() -> None:
    """A row from an older recorder carries no availability and is unscorable."""
    unverified = dc.replace(
        hand_event(0, moments=(at(1.0), at(5.0)), label="hit"),
        completed_at=None,
        label_observed_verified=False,
        attempt=None,
        supervisor_generation=None,
    )
    verified = event(1, moments=(2.0, 6.0))

    verdict = audit((unverified, verified))

    assert verdict.reasons == ("no_availability_time", None), (
        "the absent evidence is named, not repaired from observed_at"
    )
    assert released_labels((unverified, verified))[0][1] == 1, (
        "only the verified row releases a label"
    )


def test_a_label_accepted_before_its_decision_is_excluded() -> None:
    """A physically impossible ordering is reported, never scored."""
    impossible = event(0, moments=(9.0, 5.0))

    verdict = audit((impossible,))

    assert verdict.reasons == ("admission_after_availability",), (
        "the ordering fault has its own closed reason"
    )
    assert not prediction_moments((impossible,)), "it supplies no moment"


def _completion_reason(contradictory: Event) -> str | None:
    """Return the closed reason the audit names for one contradictory row.

    Returns
    -------
    str | None
        The single exclusion reason, or None when the row was accepted.
    """
    (reason,) = audit((contradictory,)).reasons
    return reason


def test_a_completion_after_its_acceptance_is_excluded() -> None:
    """A completion stamp later than the accepted reading is contradictory."""
    contradictory = dc.replace(event(0, moments=(1.0, 5.0)), completed_at=at(9.0))

    assert _completion_reason(contradictory) == "completion_after_availability", (
        "the contradictory chronology has its own closed reason"
    )


def test_a_completion_before_its_decision_is_excluded() -> None:
    """A compilation cannot finish before the decision that started it."""
    contradictory = dc.replace(event(0, moments=(10.0, 15.0)), completed_at=at(5.0))

    assert _completion_reason(contradictory) == "completion_before_admission", (
        "the contradictory chronology has its own closed reason"
    )
    assert not prediction_moments((contradictory,)), "it supplies no moment"


def test_a_batch_is_one_accepted_reading_not_one_completion_time() -> None:
    """Rows accepted by one reading share a batch whatever their completions."""
    first = dc.replace(event(0, moments=(1.0, 5.0)), completed_at=at(2.0))
    second = dc.replace(event(1, moments=(2.0, 5.0)), completed_at=at(3.0))

    assert label_batches((first, second)) == ((0, 1),), (
        "one accepted reading places both rows in one batch"
    )


@pytest.mark.parametrize(
    ("admitted", "available", "reason"),
    [
        (None, 5.0, "no_admission_time"),
        (1.0, None, "no_availability_time"),
        (9.0, 5.0, "admission_after_availability"),
        (1.0, 5.0, None),
    ],
    ids=["missing-decision", "missing-availability", "late-decision", "usable"],
)
def test_each_gate_has_its_own_closed_reason(
    admitted: float | None, available: float | None, reason: str | None
) -> None:
    """The exclusion vocabulary is exhaustive and mutually exclusive."""
    verdict = audit((event(0, moments=(admitted, available)),))

    assert verdict.reasons == (reason,), "one closed reason per row"


def test_an_unknown_outcome_is_predictable_but_never_scored() -> None:
    """A stream of unattributed rows still answers, and scores nothing."""
    events = tuple(
        event(
            index,
            moments=(float(index), float(index) + 0.5),
            label="unknown",
        )
        for index in range(4)
    )

    walk = walk_forward(events, config())

    assert len(walk.moments) == 4, "every event has a trustworthy moment pair"
    assert walk.labelled == 0, "an unknown outcome is not a label"
    assert scored(walk.timing) == 0, "and it never enters the scored population"
    assert exclusions(walk.timing) == {"unknown_outcome": 4}, "the reason is exact"


def test_a_clock_step_never_releases_a_label_early() -> None:
    """A backward availability stamp is refused rather than sorted around."""
    forward = event(0, moments=(0.0, 10.0), attempt="early")
    stepped = dc.replace(
        event(1, moments=(1.0, 5.0), attempt="late"),
        completed_at=at(5.0),
        observed_at=at(5.0),
    )

    walk = walk_forward((forward, stepped), config())

    # The single-predictor walk orders by admission, and each row's own
    # availability is what it releases; a step cannot make the later row's
    # label visible to a decision that preceded its acceptance moment.
    assert walk.moments == ((0,), (1,)), "both rows still supply their own moment"
    assert [moment for moment, _index in released_labels((forward, stepped))] == [
        at(5.0),
        at(10.0),
    ], "release order follows availability, not append order"


def test_the_events_in_a_stream_keep_their_own_identity() -> None:
    """A shared cache key is not a shared invocation."""
    first = hand_event(0, moments=(at(1.0), at(5.0)), label="hit")
    second = hand_event(0, moments=(at(2.0), at(6.0)), label="miss")

    assert first.identity == second.identity, "the cache key is identical"
    assert first.attempt != second.attempt, "the attempt is not"
    assert audit((first, second)).reasons == (None, None), "both rows are usable"
    assert typ.cast("dt.datetime", label_moment(first)) > EPOCH, (
        "each row's availability is a real recorded moment"
    )


def test_a_label_unavailable_at_its_prediction_never_trains_a_predictor() -> None:
    """A late label cannot move an earlier answer, in tuning or held-out.

    The stream is built so one identity repeats. Its first row is admitted
    before its own reading is accepted, and the second row is admitted after;
    only the second prediction may reflect the first outcome.
    """
    early = event(0, moments=(1.0, 9.0), label="hit", attempt="early")
    later = event(0, moments=(4.0, 12.0), label="miss", attempt="later")
    after = event(0, moments=(13.0, 20.0), label="miss", attempt="after")

    walk = walk_forward((early, later, after), config())
    last_outcome = walk.series["last-outcome"].probabilities

    assert last_outcome[0] is None, "the first row has no earlier outcome"
    assert last_outcome[1] is None, (
        "the first label is accepted at 9, after the second row decides at 4"
    )
    assert last_outcome[2] == pytest.approx(0.0), (
        "by the third decision both labels are released, and the miss is the "
        "identity's newest"
    )
    assert walk.applied_labels == 2, "two labels informed a later prediction"
    assert walk.unapplied_labels == 1, "the final label informs no prediction"


def test_an_unverified_label_cannot_train_either() -> None:
    """An unverified row supplies no training example, even with a label."""
    unverified = dc.replace(
        event(0, moments=(1.0, 5.0), label="hit"),
        label_observed_verified=False,
    )
    verifiable = event(0, moments=(10.0, 20.0), label="miss", attempt="b")

    walk = walk_forward((unverified, verifiable), config())

    assert walk.applied_labels == 0, (
        "the unverified label is never released, so it trains nothing"
    )
    assert walk.rows[0] is None, "and it supplies no prediction moment either"
    assert walk.series["last-outcome"].probabilities[1] is None, (
        "the later prediction sees no earlier outcome"
    )
