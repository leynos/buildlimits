"""Shared scenario for one delayed statistics reading.

Several contracts read the same sequence: two launches finish, an hour passes,
and only then does the statistics query return. The label-timing contract checks
the moment every row carries, and the label-provenance contract checks that the
same rows cannot leak a label back past that moment. Building the sequence once
here keeps both suites proving their own claim from identical evidence.
"""

import dataclasses as dc
import datetime as dt

from outcome_recorder_test_helpers import (
    OBSERVED_AT,
    baseline_state,
    begin_query,
    cache_key,
    clear_gate_event,
    complete_query,
    launch_payload,
    outcome_module,
    stats_text,
)

from buildlimits.outcome_record_format import OutcomeRecord, StoreRecord

__all__ = [
    "DELAYED_READING_AT",
    "LAUNCH_COMPLETIONS",
    "delayed_reading_rows",
    "outcome_rows",
]

DELAYED_READING_AT = OBSERVED_AT + dt.timedelta(hours=1)
"""The moment a delayed reading returned, an hour after its launches finished."""

LAUNCH_COMPLETIONS = (
    OBSERVED_AT + dt.timedelta(minutes=3),
    OBSERVED_AT + dt.timedelta(minutes=1),
)
"""Two launches that finished out of order, both well before the reading."""


def delayed_reading_rows(
    *, completions: tuple[dt.datetime, ...], reading_at: dt.datetime
) -> tuple[StoreRecord, ...]:
    """Run one delayed statistics reading over completed cached launches.

    Each launch finishes at its own ``completions`` moment, an hour before the
    reading returns. The rows the reading emits must carry ``reading_at`` and
    never a launch's completion, so both the label-timing contract and the
    label-provenance contract can build their history from this one sequence.

    Parameters
    ----------
    completions : tuple[datetime, ...]
        The wall-clock moment each launch completed, in launch order.
    reading_at : datetime
        The moment the statistics query returned.

    Returns
    -------
    tuple[StoreRecord, ...]
        The reading's records, outcome rows and window summary included.

    Examples
    --------
    One completion produces one outcome row and its window summary.
    """
    recorder = outcome_module("outcome_recorder")
    state = baseline_state(recorder)
    for index, completed_at in enumerate(completions):
        group_id = f"delayed-{index}"
        state, _ = recorder.advance(
            state,
            recorder.Launched(
                group_id=group_id,
                is_cached=True,
                payload=launch_payload(
                    recorder,
                    dc.replace(cache_key(), argument_digest=f"{index:064x}"),
                ),
                now=float(index),
                admitted_at=OBSERVED_AT,
                observed_at=completed_at,
            ),
        )
        state, _ = recorder.advance(
            state,
            recorder.Ended(
                group_id=group_id,
                is_clean=True,
                now=float(index) + 0.25,
                observed_at=completed_at,
            ),
        )
    state, _ = begin_query(state, recorder, clear_gate_event(recorder, 10.0))
    _state, output = complete_query(
        state,
        recorder,
        stats_text(
            compile_requests=len(completions),
            requests_executed=len(completions),
            cache_hits=len(completions),
        ),
        10.1,
        observed_at=reading_at,
    )
    return output.records


def outcome_rows(records: tuple[StoreRecord, ...]) -> list[OutcomeRecord]:
    """Return one reading's keyed outcome rows, in the order it emitted them.

    Returns
    -------
    list[OutcomeRecord]
        The keyed outcome rows, excluding any window summary.

    Examples
    --------
    A reading that emits a summary returns only its outcome rows here.
    """
    return [record for record in records if isinstance(record, OutcomeRecord)]
