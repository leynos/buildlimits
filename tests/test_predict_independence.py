"""The independent-label gate and the population it reports.

Two questions are answered here. Which rows may count toward the minimum-label
gate, where only a complete attempt pair that no correlation exclusion rejects
is independent evidence? And what does the published ``uncorrelated`` count
mean, given that it exists to say how much of a history rests on independence a
repeated append cannot check?

The rows that fail these questions are not discarded: a legacy row still
replays and scores. It simply does not claim to be independent evidence, and
the count names only the labelled, verified rows that justify it.
"""

import collections.abc as cabc
import dataclasses as dc
import datetime as dt
import typing as typ

from predict_test_helpers import hand_event, predict_config
from predict_timing_helpers import event

from buildlimits.predict.evaluation import evaluate
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.sufficiency import (
    STATUS_INSUFFICIENT,
    STATUS_INSUFFICIENT_AVAILABILITY,
    STATUS_INSUFFICIENT_HELDOUT,
)
from buildlimits.predict.timing import audit

if typ.TYPE_CHECKING:
    from buildlimits.predict.events import Label
    from buildlimits.predict.harness import Walk


def test_repeated_attempts_do_not_satisfy_the_label_gate() -> None:
    """A label gate counts independent invocations, not repeated appends.

    Forty attempts appended twice each give eighty labelled rows but only forty
    independent ones. Counting the raw rows would let a history that is mostly
    retries clear a gate of fifty and reach a verdict on evidence it does not
    have.
    """
    events = tuple(
        dc.replace(
            hand_event(
                attempt,
                moments=(
                    EPOCH + dt.timedelta(seconds=attempt),
                    EPOCH + dt.timedelta(seconds=attempt + 1),
                ),
                label="hit" if attempt % 2 else "miss",
            ),
            attempt=f"g{attempt}",
        )
        for attempt in range(40)
        for _copy in range(2)
    )
    config = dc.replace(predict_config(), minimum_labelled_events=50)

    walk = walk_forward(events, config)

    assert walk.labelled == 80, "every row carries a label"
    assert walk.usable_labelled == 40, "only the distinct attempts are evidence"
    assert evaluate(walk, config).status == STATUS_INSUFFICIENT, (
        "the gate counts independent labels, so the retries cannot clear it"
    )


@dc.dataclass(frozen=True, slots=True)
class _Shape:
    """The parts of a hand-built row that are not its attempt identity."""

    start: int = 0
    verified: bool = True
    label: str = "hit"


def _rows(
    count: int, shape: _Shape, attempts: cabc.Callable[[int], str | None]
) -> tuple[Event, ...]:
    """Return ``count`` hand-built rows with the attempt the caller names.

    The rows share one shape because every case in this module differs only in
    its identity and timing evidence: a correlated row names a distinct
    attempt, and a legacy row names none.

    Returns
    -------
    tuple[Event, ...]
        The rows, in append order.
    """
    return tuple(
        dc.replace(
            hand_event(
                shape.start + index,
                moments=(
                    EPOCH + dt.timedelta(seconds=shape.start + index),
                    EPOCH + dt.timedelta(seconds=shape.start + index + 1),
                ),
                label=typ.cast("Label", shape.label),
                verified=shape.verified,
            ),
            attempt=attempts(index),
        )
        for index in range(count)
    )


def _correlated(
    count: int, *, verified: bool = True, prefix: str = "c"
) -> tuple[Event, ...]:
    """Return ``count`` rows that each name a distinct invocation.

    Returns
    -------
    tuple[Event, ...]
        The correlated rows, in append order.
    """
    return _rows(
        count,
        _Shape(verified=verified),
        lambda index: f"{prefix}{index}",
    )


def _legacy(count: int, *, start: int = 0, label: str = "hit") -> tuple[Event, ...]:
    """Return ``count`` rows that carry a verified time but no attempt pair.

    Each row keeps the moment its label was accepted and loses only the
    correlation identity, which is what a version 3 or 4 row looks like after
    extraction.

    Returns
    -------
    tuple[Event, ...]
        The legacy rows, in append order.
    """
    return tuple(
        dc.replace(row, supervisor_generation=None)
        for row in _rows(
            count,
            _Shape(start=start, label=label),
            lambda _index: None,
        )
    )


def test_a_legacy_row_replays_but_does_not_claim_independence() -> None:
    """A row with no attempt pair is evidence of a label, not of independence.

    The compatibility rule keeps such a row scorable: it proves when its label
    was accepted, which is the question the replay asks. It cannot prove that
    it is a distinct invocation, so it does not count toward a gate whose
    purpose is to require independent labels. Otherwise a legacy history could
    satisfy the gate with rows whose duplication the document cannot rule out.
    """
    legacy = _legacy(12)
    config = dc.replace(predict_config(), minimum_labelled_events=10)

    walk = walk_forward(legacy, config)

    assert walk.labelled == 12, "every legacy row carries a label"
    assert walk.independent_labelled == 0, "none of them proves its independence"
    assert walk.usable_labelled == 0, "and none of them counts toward the gate"
    assert sum(len(group) for group in walk.moments) == 12, "each row still replays"
    assert evaluate(walk, config).status == STATUS_INSUFFICIENT, (
        "the gate is not cleared by rows whose independence is unknown"
    )


def test_a_mixed_history_cannot_compose_one_gate_out_of_two_populations() -> None:
    """Independent-but-unverified labels plus scored legacy rows are not enough.

    Twenty correlated rows prove their independence but not their timing, and
    twelve legacy rows prove their timing but not their independence. Either
    population alone is large enough to look like evidence, and the legacy rows
    do supply scored held-out rows. The gate counts their intersection, which
    is empty, so the verdict names the availability evidence it is short of
    rather than reporting a comparison the history cannot support.
    """
    unverified = _correlated(20, verified=False, prefix="u")
    legacy = _legacy(12, start=100, label="miss")
    config = dc.replace(predict_config(), minimum_labelled_events=10)

    walk = walk_forward(unverified + legacy, config)

    assert walk.labelled == 32, "both populations carry labels"
    assert walk.independent_labelled == 20, "the correlated rows are independent"
    assert walk.usable_labelled == 0, "but none of the rows is jointly usable"
    assert evaluate(walk, config).status == STATUS_INSUFFICIENT_AVAILABILITY, (
        "the verdict names the timing evidence the history is short of"
    )


def test_a_row_missing_both_times_counts_as_neither_half() -> None:
    """One closed reason must not stand as evidence for the other half.

    The audit reports the first failure only, so a row with neither a decision
    nor a verified label is excluded as ``no_admission_time``. Reading that
    reason as proof that its availability was fine would count it as
    availability-usable, and the verdict would name an admission shortage for
    a history that is short of both. Five fully usable rows plus fifteen that
    lack both times must not report twenty availability-usable labels.
    """
    usable = _correlated(5, prefix="a")
    missing_both = tuple(
        dc.replace(
            hand_event(
                50 + index,
                moments=(None, EPOCH + dt.timedelta(seconds=100 + index)),
                label="hit",
                verified=False,
            ),
            attempt=f"b{index}",
        )
        for index in range(15)
    )
    config = dc.replace(predict_config(), minimum_labelled_events=10)

    walk = walk_forward(usable + missing_both, config)

    assert walk.independent_labelled == 20, "all twenty name a complete pair"
    assert walk.usable_labelled == 5, "only five carry both times"
    assert walk.admission_usable_labelled == 5, "five record a decision"
    assert walk.availability_usable_labelled == 5, (
        "a row missing its decision cannot lend availability evidence either"
    )
    assert evaluate(walk, config).status == STATUS_INSUFFICIENT, (
        "the timing halves tie, so neither shortage is named"
    )


def _beside_usable(extra: Event) -> Walk:
    """Return the walk for five usable rows beside one defective row.

    Returns
    -------
    Walk
        The replay of the six rows under the test thresholds.
    """
    config = dc.replace(predict_config(), minimum_labelled_events=10)
    return walk_forward((*_correlated(5, prefix="a"), extra), config)


def test_an_unverified_row_cannot_lend_its_unordered_decision() -> None:
    """The first closed reason is not a statement about the other half.

    The row is unverified and its admission (30) follows its own observation
    (20), so the audit reports ``no_availability_time``. Its admission still
    cannot count as a usable decision: ``admission_moment`` rejects the
    ordering, and the diagnostic population must agree with the replay about
    what is usable.
    """
    unordered = dc.replace(
        event(0, moments=(1.0, None)),
        admitted_at=EPOCH + dt.timedelta(seconds=30),
        observed_at=EPOCH + dt.timedelta(seconds=20),
        label_observed_verified=False,
    )
    walk = _beside_usable(unordered)

    assert walk.admission_usable_labelled == 5, (
        "an admission after its own observation is not a usable decision"
    )
    assert walk.availability_usable_labelled == 5, "nor does it lend availability"


def test_a_late_completion_withholds_availability_despite_another_reason() -> None:
    """A completion later than the label is its own defect, whatever came first.

    The row's admission is also after its availability, so the audit names
    ``admission_after_availability``. Its completion is later still, so the
    availability count must not take the audit's first reason as proof that the
    label time is sound.
    """
    contradictory = dc.replace(
        event(0, moments=(1.0, 5.0)),
        admitted_at=EPOCH + dt.timedelta(seconds=30),
        completed_at=EPOCH + dt.timedelta(seconds=25),
    )
    walk = _beside_usable(contradictory)

    assert walk.admission_usable_labelled == 5, "the late decision is not usable"
    assert walk.availability_usable_labelled == 5, (
        "a completion after the label withholds the availability too"
    )


def test_legacy_copies_cannot_decide_the_held_out_comparison() -> None:
    """A repeated legacy row is a copy of one observation, not evidence.

    The tuning period is large enough and independent enough to clear the label
    gate. The held-out period is a run of copies of one verified legacy row:
    each copy is scorable, so nothing stops it from filling the held-out span,
    but none of them names a distinct invocation. A decisive comparison needs
    independent held-out rows, so the verdict reports that it has too little
    held-out evidence rather than ranking predictors on one repeated
    observation.
    """
    training = tuple(
        dc.replace(row, label=typ.cast("Label", "hit" if index % 2 else "miss"))
        for index, row in enumerate(_correlated(24, prefix="t"))
    )
    legacy_copies = tuple(
        dc.replace(
            hand_event(
                100,
                moments=(
                    EPOCH + dt.timedelta(seconds=100),
                    EPOCH + dt.timedelta(seconds=101),
                ),
                label="hit",
            ),
            attempt=None,
            supervisor_generation=None,
        )
        for _copy in range(60)
    )
    config = dc.replace(
        predict_config(),
        minimum_labelled_events=10,
        train_fraction=0.29,
        block_size=10,
    )

    walk = walk_forward(training + legacy_copies, config)

    assert evaluate(walk, config).status == STATUS_INSUFFICIENT_HELDOUT, (
        "one repeated legacy observation cannot decide the comparison"
    )


def test_an_uncorrelated_count_states_only_the_labels_it_means() -> None:
    """An unknown outcome is not a label, so it cannot inflate the count.

    The count exists to say how much of the label gate rests on independence
    that a repeated append cannot check. A row with no hit or miss outcome
    carries no label at all, and an unverified row has no moment to place, so
    neither belongs in that population.
    """
    legacy = dc.replace(
        event(0, moments=(1.0, 5.0)),
        attempt=None,
        supervisor_generation=None,
    )
    unknown = dc.replace(
        event(1, moments=(2.0, 6.0), label="unknown"),
        attempt=None,
        supervisor_generation=None,
    )

    verdict = audit((legacy, unknown))

    assert verdict.uncorrelated == 1, "only the labelled legacy row is counted"


def test_a_labelled_unverified_row_is_not_counted_as_uncorrelated() -> None:
    """A row that cannot prove its time is withheld, not uncorrelated.

    It is already reported through the availability gate. Counting it here as
    well would state the same absence twice and overstate the population whose
    independence is merely unknown.
    """
    withheld = dc.replace(
        event(0, moments=(1.0, 5.0)),
        label_observed_verified=False,
        attempt=None,
        supervisor_generation=None,
    )

    verdict = audit((withheld,))

    assert verdict.uncorrelated == 0, "a withheld label is not counted here"
    assert verdict.reasons == ("no_availability_time",), (
        "the availability gate reports it instead"
    )
