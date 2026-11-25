"""Label loss is measured per sccache generation, and only where it is proven.

Issue #68 asks for label-loss counts by sccache generation. A row can carry
that dimension only when its reading stood between two boundaries that named
one server: a changed, unreadable, baseline or overflowed window has no single
generation, and its rows state null rather than a guess. These tests prove both
directions through the real recorder, the real encoder and the real store
accounting, so a breakdown by generation is a measurement rather than an
inference from a neighbouring window.
"""

import dataclasses as dc
import typing as typ
from types import ModuleType

from outcome_reading_test_helpers import outcome_rows
from outcome_recorder_test_helpers import (
    WindowSetup,
    begin_query,
    state_with_window,
)
from outcome_test_helpers import (
    GENERATION,
    OBSERVED_AT,
    cache_key,
    clear_gate_event,
    launch_payload,
    outcome_module,
    stats_text,
)

from buildlimits import outcome_key
from buildlimits.outcome_record_format import OutcomeRecord
from buildlimits.predict.accounting import store_accounting

if typ.TYPE_CHECKING:
    from buildlimits.outcome_attribution import Generation

SALT = outcome_key.Salt(key=b"s" * 32, salt_id="fixed-salt")


def _digest(generation: Generation = GENERATION) -> str:
    """Return the correlation key for one generation under the test salt.

    Returns
    -------
    str
        The salted digest the shell would compute.
    """
    return outcome_key.generation_digest(
        generation.invocation, generation.pid, generation.starttime, SALT
    )


def _window_state(count: int = 1) -> tuple[object, ModuleType]:
    """Build a recorder holding ``count`` completed cached launches.

    Returns
    -------
    tuple[object, ModuleType]
        The idle state and the recorder module that produced it.
    """
    recorder = outcome_module("outcome_recorder")
    payload = launch_payload(recorder, cache_key())
    setup = WindowSetup(payload=payload, count=count)
    return state_with_window(recorder, setup), recorder


@dc.dataclass(frozen=True, slots=True)
class _Reading:
    """The values one completed statistics reading reports.

    ``count`` is the number of launches the window holds, ``after`` is the
    server identity the closing boundary observed, and ``digest`` is the
    correlation key the driver derived from it. They vary together in these
    tests, so they travel as one value rather than three arguments.
    """

    count: int
    after: Generation = GENERATION
    digest: str | None = None


def _polled(
    state: object, recorder: ModuleType, reading: _Reading
) -> list[OutcomeRecord]:
    """Complete one reading over a window of cached launches.

    Returns
    -------
    list[OutcomeRecord]
        The reading's keyed outcome rows, with its window summary excluded.
    """
    ports = outcome_module("outcome_ports")
    count = reading.count
    _state, output = recorder.advance(
        state,
        recorder.PollResult(
            now=2.0,
            result=ports.StatisticsText(
                stats_text(
                    compile_requests=count,
                    requests_executed=count,
                    cache_hits=count,
                )
            ),
            after_generation=reading.after,
            after_generation_digest=reading.digest,
            observed_at=OBSERVED_AT,
        ),
    )
    return outcome_rows(output.records)


def test_an_attributing_reading_copies_its_generation_onto_every_row() -> None:
    """A window whose two boundaries agree names one generation on each row.

    The digest is the reading's own, so every row it emits shares it. That is
    what makes a per-generation breakdown a measurement: the rows and the
    generation were produced by the same transition.
    """
    state, recorder = _window_state(2)
    started, _ = begin_query(state, recorder, clear_gate_event(recorder, 1.0))

    rows = _polled(started, recorder, _Reading(2, digest=_digest()))

    assert len(rows) == 2, "both launches are attributed"
    assert {row.generation_digest for row in rows} == {_digest()}, (
        "every attributed row names the one generation"
    )


def test_a_changed_generation_leaves_every_row_without_a_digest() -> None:
    """A window whose servers differ names no generation, and never guesses one.

    The recorder refuses such a window and publishes its launches as
    unattributed. Those rows still reach the store, so they must state that no
    single generation can be attributed rather than borrow either boundary's.
    """
    replacement = outcome_module("outcome_attribution").Generation("run-b", 11, 21)
    state, recorder = _window_state(1)
    started, _ = begin_query(state, recorder, clear_gate_event(recorder, 1.0))

    rows = _polled(started, recorder, _Reading(1, replacement, _digest(replacement)))

    assert len(rows) == 1, "the refused window still accounts for its launch"
    assert rows[0].generation_digest is None, (
        "a changed window cannot attribute its rows to one server"
    )
    assert rows[0].outcome_reason == "generation_changed", (
        "the reason still names the refusal"
    )


def test_a_baseline_window_states_no_generation() -> None:
    """The first reading accounts for a window it never measured.

    A baseline is refused rather than attributed, so its rows carry the refusal
    reason and no digest: the reading observed a server, but it never proved
    that server produced these launches.
    """
    recorder = outcome_module("outcome_recorder")
    payload = launch_payload(recorder, cache_key())
    state = recorder.Idle()
    state, _ = recorder.advance(
        state,
        recorder.Launched(group_id="first", is_cached=True, payload=payload, now=0.0),
    )
    started, _ = begin_query(state, recorder, clear_gate_event(recorder, 1.0))

    rows = _polled(started, recorder, _Reading(0, digest=_digest()))

    assert rows, "the unmeasured window is still accounted"
    assert {row.generation_digest for row in rows} == {None}, (
        "a baseline measured no window, so it attributes no generation"
    )
    assert {row.outcome_reason for row in rows} == {"no_baseline"}, (
        "the refusal is named"
    )


def test_an_overflowed_window_states_no_generation() -> None:
    """An overflow flush has no reading at all, so no generation was observed."""
    recorder = outcome_module("outcome_recorder")
    payload = launch_payload(recorder, cache_key())
    # The 65th append is the one that overflows, so the window starts at 64.
    state = state_with_window(recorder, WindowSetup(payload=payload, count=64))
    _state, output = recorder.advance(
        state,
        recorder.Launched(
            group_id="overflow", is_cached=True, payload=payload, now=100.0
        ),
    )
    rows = outcome_rows(output.records)

    assert len(rows) == 64, "the whole window is accounted at the overflow"
    assert {row.generation_digest for row in rows} == {None}, (
        "no reading produced these rows, so none can name a generation"
    )
    assert {row.outcome_reason for row in rows} == {"window_overflow"}, (
        "the reason still names why they were unlabelled"
    )


def test_store_accounting_counts_each_generation_and_reconciles() -> None:
    """The breakdown by generation sums to the rows and separates label loss.

    Two generations each contribute one labelled and one unlabelled row, and a
    third row states no generation at all. The map must show exactly those
    three buckets, sum to the outcome rows, and keep the unattributed row out
    of either generation's count.
    """
    first = _digest()
    second = _digest(outcome_module("outcome_attribution").Generation("run-b", 11, 21))
    base = OutcomeRecord(
        key=cache_key(),
        outcome="hit",
        origin="registry",
        runtime_seconds=0.1,
        observed_at=OBSERVED_AT,
        parser_class="cacheable",
        child_sighting="unavailable",
        child_samples=0,
        outcome_reason="none",
        outcome_attribution="solo",
    )
    records = (
        dc.replace(base, outcome="miss", generation_digest=first),
        dc.replace(
            base,
            outcome="unattributed",
            outcome_reason="mixed_window",
            generation_digest=first,
        ),
        dc.replace(base, outcome="hit", generation_digest=second),
        dc.replace(
            base,
            outcome="unattributed",
            outcome_reason="generation_changed",
            generation_digest=None,
        ),
    )

    accounting = store_accounting(records)

    assert accounting.by_generation == {
        first: 2,
        second: 1,
        "unrecorded": 1,
    }, "each generation is counted on its own, with one absent bucket"
    assert sum(accounting.by_generation.values()) == accounting.outcome_records, (
        "the breakdown accounts for every outcome row"
    )
    assert accounting.by_generation[first] == 2, (
        "the first generation's labelled and unlabelled rows are both counted"
    )


def test_the_generation_digest_is_stable_and_genuinely_separating() -> None:
    """The digest names one triple, and a different triple names another.

    A process id alone is reused by the kernel and a start time alone is not an
    identity, so the digest joins all three parts. This pins both properties:
    the same triple always digests alike, and changing any one part changes the
    key, so two generations can never be merged into one bucket.
    """
    identity = ("run-a", 10, 20)
    variants = (("run-b", 10, 20), ("run-a", 11, 20), ("run-a", 10, 21))

    assert outcome_key.generation_digest(*identity, SALT) == _digest(), (
        "the same identity always produces the same key"
    )
    for variant in variants:
        assert outcome_key.generation_digest(*variant, SALT) != _digest(), (
            f"changing {variant} must name a different generation"
        )


def test_a_different_salt_does_not_join_to_this_hosts_history() -> None:
    """The digest is scoped to one store's salt, so it leaks no process identity."""
    other = outcome_key.Salt(key=b"t" * 32, salt_id="other-salt")

    assert outcome_key.generation_digest("run-a", 10, 20, other) != _digest(), (
        "a different salt cannot reproduce another store's generation key"
    )
    assert len(_digest()) == 64, "the key is a full-length digest"
