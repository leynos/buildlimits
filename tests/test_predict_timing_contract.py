"""The per-compilation timing and correlation contract for replay.

Every test here uses injected clocks. The producer's monotonic and UTC seams,
the recorder's poll boundary and the store's wall clock are supplied by the
test, so a scenario can state exact moments for a queue delay, a delayed
statistics reading or a concurrent out-of-order completion and then assert
exactly which labels a later decision could learn.

The scenarios are the issue's regressions:

- A completes at 2, B decides at 3, and A's attribution is accepted at 5.
  B cannot learn A's label; a decision after 5 can; a decision at exactly 5
  still obeys the strict-before rule.
- Two invocations with one cache key that finish out of launch order keep
  their own attempt identities and availability times.
- Delayed writes, duplicate or conflicting appends, restart boundaries and a
  clock step never move a label into an earlier prediction's history.
"""

import dataclasses as dc
import datetime as dt
import typing as typ
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from predict_test_helpers import hand_event
from render_helpers import empty_document

from buildlimits.predict.config_loader import load_config
from buildlimits.predict.dataset import Snapshot, Source
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.history import (
    label_batches,
    prediction_moments,
    released_labels,
)
from buildlimits.predict.render import render_markdown
from buildlimits.predict.snapshot_store import load_snapshot, save_snapshot
from buildlimits.predict.timing import audit

if typ.TYPE_CHECKING:
    from buildlimits.predict.config import Config

MINUTE = dt.timedelta(minutes=1)
SECOND = dt.timedelta(seconds=1)


def _at(seconds: float) -> dt.datetime:
    """Return the epoch plus the supplied seconds."""
    return EPOCH + dt.timedelta(seconds=seconds)


def _event(
    index: int,
    *,
    admitted: float | None,
    available: float | None,
    label: str = "hit",
    attempt: str | None = None,
) -> Event:
    """Build one event with explicit decision and availability moments.

    The historical observation time is the availability time, which is what a
    version 3 recorder writes for a row it accepted at one reading.
    """
    moment = None if available is None else _at(available)
    return Event(
        identity=f"{index:064d}"[:64],
        origin="git",
        admitted_at=None if admitted is None else _at(admitted),
        observed_at=moment if moment is not None else _at(0),
        label=typ.cast("typ.Any", label),
        runtime_seconds=1.0,
        completed_at=moment,
        label_available_at=moment,
        attempt=f"g{index}" if attempt is None else attempt,
        supervisor_generation="generation-a",
    )


# --------------------------------------------------------------------------
# The reviewer's regression: A completes at 2, B decides at 3, A is accepted
# at 5. B must not learn A; a decision after 5 must; a decision at 5 must not.
# --------------------------------------------------------------------------


def _reviewer_stream() -> tuple[Event, ...]:
    """Return A, B and the two decisions either side of A's availability."""
    return (
        # A: admitted at 1, completes at 2, attribution accepted at 5.
        _event(0, admitted=1.0, available=5.0),
        # B: decided at 3, its own label accepted later.
        _event(1, admitted=3.0, available=9.0, label="miss"),
        # C: decided at 6, after A became available but B had not.
        _event(2, admitted=6.0, available=12.0),
        # D: decided at exactly 5, the availability instant itself.
        _event(3, admitted=5.0, available=20.0, label="miss"),
    )


def test_a_decision_before_a_label_became_available_cannot_learn_it() -> None:
    """B decides at 3, A is accepted at 5, so B's history is empty."""
    stream = _reviewer_stream()

    releases = released_labels(stream)
    assert [index for _moment, index in releases] == [0, 1, 2, 3], (
        "every verified label is releasable in acceptance order"
    )
    walk = walk_forward(stream, _config())
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
    later = _event(9, admitted=5.5, available=30.0, label="miss")
    walk = walk_forward((*_reviewer_stream(), later), _config())

    assert typ.cast("typ.Any", walk.rows[4]).load == 1, (
        "only A was accepted before the half-second-later decision"
    )
    assert typ.cast("typ.Any", walk.rows[4]).age_seconds is None, (
        "the identity has no earlier label of its own"
    )


def test_a_label_is_never_released_at_its_completion_time() -> None:
    """Completion is retained for provenance and never used as availability."""
    slow_accept = dc.replace(
        _event(0, admitted=1.0, available=5.0), completed_at=_at(2.0)
    )
    between = _event(1, admitted=3.0, available=9.0, label="miss")

    walk = walk_forward((slow_accept, between), _config())

    assert typ.cast("typ.Any", walk.rows[1]).load == 0, (
        "the label completed at 2 but was accepted at 5, so B cannot read it"
    )
    assert slow_accept.completed_at == _at(2.0), "the completion time is retained"


# --------------------------------------------------------------------------
# Concurrent invocations with one cache key, finishing out of launch order.
# --------------------------------------------------------------------------


def test_concurrent_identical_invocations_keep_their_own_timing() -> None:
    """Two attempts for one identity never share a completion or availability."""
    first = hand_event(
        0,
        admitted_at=_at(1.0),
        observed_at=_at(9.0),
        label="hit",
        attempt="attempt-a",
    )
    second = hand_event(
        0,
        admitted_at=_at(2.0),
        observed_at=_at(4.0),
        label="miss",
        attempt="attempt-b",
    )

    walk = walk_forward((first, second), _config())
    verdict = audit((first, second))

    assert first.identity == second.identity, "both invocations share one key"
    assert first.label_available_at == _at(9.0), "the slow attempt keeps its own"
    assert second.label_available_at == _at(4.0), "the fast attempt keeps its own"
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
        _event(
            position,
            admitted=-1.0,
            available=float(finish),
            attempt=f"attempt-{position}",
        )
        for position, finish in enumerate(finishes)
    )

    verdict = audit(events)

    assert verdict.reasons == (None,) * len(events), "distinct attempts never collide"
    assert [event.label_available_at for event in events] == [
        _at(float(finish)) for finish in finishes
    ], "each row's acceptance is its own, whatever order the launches finished"
    assert sorted(
        typ.cast("dt.datetime", event.label_available_at) for event in events
    ) == [_at(float(finish)) for finish in ordered], "the set of moments is unchanged"


# --------------------------------------------------------------------------
# Delayed writes, duplicates, restarts and clock steps.
# --------------------------------------------------------------------------


def test_an_exact_duplicate_append_is_an_idempotent_duplicate() -> None:
    """A retried append with identical evidence does not create a second label."""
    original = _event(0, admitted=1.0, available=5.0)
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
    first = _event(0, admitted=1.0, available=5.0, label="hit", attempt="same")
    second = _event(0, admitted=1.0, available=5.0, label="miss", attempt="same")

    verdict = audit((first, second))

    assert verdict.conflicts == 2, "both copies are conflicts"
    assert verdict.reasons == ("conflicting_attempt", "conflicting_attempt"), (
        "a contradictory pair is excluded rather than resolved"
    )
    assert released_labels((first, second)) == [], "neither releases a label"
    assert walk_forward((first, second), _config()).moments == (), (
        "neither supplies a prediction moment either"
    )


def test_a_restart_generation_separates_reused_group_identifiers() -> None:
    """The same attempt token in a new supervisor is not the same attempt."""
    before = _event(0, admitted=1.0, available=5.0, attempt="g1")
    after = dc.replace(
        _event(0, admitted=1.0, available=5.0, attempt="g1"),
        supervisor_generation="generation-b",
    )

    verdict = audit((before, after))

    assert verdict.reasons == (None, None), "two generations never join"
    assert before.attempt == after.attempt, "the token alone is reused"


def test_an_unverified_row_is_excluded_rather_than_backdated() -> None:
    """A row from an older recorder carries no availability and is unscorable."""
    unverified = dc.replace(
        hand_event(0, admitted_at=_at(1.0), observed_at=_at(5.0), label="hit"),
        completed_at=None,
        label_available_at=None,
        attempt=None,
        supervisor_generation=None,
    )
    verified = _event(1, admitted=2.0, available=6.0)

    verdict = audit((unverified, verified))

    assert verdict.reasons == ("no_availability_time", None), (
        "the absent evidence is named, not repaired from observed_at"
    )
    assert released_labels((unverified, verified))[0][1] == 1, (
        "only the verified row releases a label"
    )


def test_a_label_accepted_before_its_decision_is_excluded() -> None:
    """A physically impossible ordering is reported, never scored."""
    impossible = _event(0, admitted=9.0, available=5.0)

    verdict = audit((impossible,))

    assert verdict.reasons == ("admission_after_availability",), (
        "the ordering fault has its own closed reason"
    )
    assert prediction_moments((impossible,)) == (), "it supplies no moment"


# --------------------------------------------------------------------------
# Producer through store, snapshot and replay, with injected clocks.
# --------------------------------------------------------------------------


def test_the_snapshot_round_trip_preserves_identity_and_chronology(
    tmp_path: Path,
) -> None:
    """A verified history survives save and load with every moment intact."""
    events = _reviewer_stream()
    path = tmp_path / "history.jsonl"
    save_snapshot(path, _snapshot(events))

    loaded = load_snapshot(path)

    assert loaded.events == events, "every timing field round-trips exactly"
    assert [event.attempt for event in loaded.events] == [
        event.attempt for event in events
    ], "the attempt identities survive"


def test_an_old_format_snapshot_round_trips_without_fabricated_timing(
    tmp_path: Path,
) -> None:
    """A version 2 document keeps its rows and gains no invented moments."""
    path = tmp_path / "history.jsonl"
    save_snapshot(path, _snapshot((_event(0, admitted=1.0, available=5.0),)))
    document = typ.cast(
        "dict[str, object]",
        __import__("json").loads(path.read_text(encoding="utf-8")),
    )
    document["snapshot_version"] = 2
    event = typ.cast(
        "dict[str, object]", typ.cast("list[object]", document["events"])[0]
    )
    for absent in (
        "completed_at",
        "label_available_at",
        "attempt",
        "supervisor_generation",
    ):
        event.pop(absent)
    path.write_text(__import__("json").dumps(document), encoding="utf-8")

    loaded = load_snapshot(path)

    assert loaded.events[0].label_available_at is None, "no availability is invented"
    assert loaded.events[0].attempt is None, "no attempt identity is invented"
    stats = loaded.stats()
    assert stats.scored == 0, "an old-format row is unscorable"
    assert stats.excluded == {"no_availability_time": 1}, "the gap is visible"


def test_the_benchmark_reports_each_gate_separately(tmp_path: Path) -> None:
    """Dataset counts separate labels, decisions, availability and scoring."""
    snapshot = _snapshot((
        _event(0, admitted=1.0, available=5.0),
        _event(1, admitted=None, available=6.0),
        _event(2, admitted=3.0, available=None),
    ))

    stats = snapshot.stats()

    assert stats.labelled == 3, "every row carries a hit or miss label"
    assert stats.admission_recorded == 2, "two rows record a decision"
    assert stats.availability_recorded == 2, "two rows record availability"
    assert stats.scored == 1, "only the row that passes both gates is scored"
    assert stats.excluded == {
        "no_admission_time": 1,
        "no_availability_time": 1,
    }, "each exclusion names its own gate"


def test_the_report_states_the_scored_population_and_its_exclusions() -> None:
    """The rendered report never hides a timing exclusion behind a label count."""
    document = empty_document()
    stats = typ.cast("dict[str, object]", typ.cast("dict", document["data"])["stats"])
    stats.update({
        "labelled": 3,
        "admission_recorded": 3,
        "admission_usable": 2,
        "availability_recorded": 1,
        "availability_usable": 1,
        "availability_missing": 2,
        "scored": 1,
        "excluded": {"no_availability_time": 2},
    })

    markdown = render_markdown(document)

    assert (
        "- Label availability: 1 recorded, 1 usable for a prediction, 2 missing"
    ) in markdown, "availability has its own coverage line"
    assert (
        "- Scored records: 1 (exclusions: no_availability_time=2; "
        "0 duplicate attempts, 0 conflicting attempts)"
    ) in markdown, "exclusions are counted by their closed reason"


def _config() -> Config:
    """Return the pinned parameters with a zero-second load window.

    Returns
    -------
    Config
        Parameters that make every feature row independently checkable.
    """
    return dc.replace(load_config(), load_window_seconds=60.0)


def _snapshot(events: tuple[Event, ...]) -> Snapshot:
    """Return a snapshot holding ``events`` in append order."""
    return Snapshot(
        created_at=_at(0),
        source=Source(
            salt_id="timing-salt",
            written_at=_at(0),
            records_total=len(events),
            outcome_records=len(events),
            summary_records=0,
        ),
        journal=None,
        events=events,
        gaps=(),
    )


def test_a_batch_is_one_accepted_reading_not_one_completion_time() -> None:
    """Rows accepted by one reading share a batch whatever their completions."""
    first = dc.replace(_event(0, admitted=1.0, available=5.0), completed_at=_at(2.0))
    second = dc.replace(_event(1, admitted=2.0, available=5.0), completed_at=_at(3.0))

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
    verdict = audit((_event(0, admitted=admitted, available=available),))

    assert verdict.reasons == (reason,), "one closed reason per row"


def test_an_unknown_outcome_is_predictable_but_never_scored() -> None:
    """A stream of unattributed rows still answers, and scores nothing."""
    events = tuple(
        _event(
            index, admitted=float(index), available=float(index) + 0.5, label="unknown"
        )
        for index in range(4)
    )

    walk = walk_forward(events, _config())

    assert len(walk.moments) == 4, "every event has a trustworthy moment pair"
    assert walk.labelled == 0, "an unknown outcome is not a label"
    assert walk.timing.scored() == 0, "and it never enters the scored population"
    assert walk.timing.exclusions() == {"unknown_outcome": 4}, "the reason is exact"
