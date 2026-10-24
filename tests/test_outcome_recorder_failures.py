"""Closed reading-reason outcomes from the pure recorder state machine."""

import typing as typ

import pytest
from outcome_recorder_test_helpers import (
    GENERATION,
    WindowSetup,
    begin_query,
    cache_key,
    clear_gate_event,
    launch_payload,
    outcome_module,
    state_with_window,
    stats_reading,
    stats_text,
)

if typ.TYPE_CHECKING:
    from types import ModuleType

pytestmark = pytest.mark.xfail(
    strict=True, reason="EP-M4 red contracts precede the outcome recorder"
)


def _reading_result(failure: str, recorder_ports: ModuleType) -> tuple[object, object]:
    """Return the fake-port reading that reaches one named D13 reason."""
    changed_generation = outcome_module("outcome_attribution").Generation(
        "run-b", 11, 21
    )
    generation_failure = recorder_ports.GenerationFailure("generation_unavailable")
    result_by_failure = {
        "timeout": (recorder_ports.QueryFailure("timeout"), GENERATION),
        "read_error": (recorder_ports.QueryFailure("read_error"), GENERATION),
        "parse_error": (recorder_ports.StatisticsText("not JSON"), GENERATION),
        "unsupported_version": (
            recorder_ports.StatisticsText('{"version":"9.9.9","stats":{}}'),
            GENERATION,
        ),
        "generation_unavailable": (
            recorder_ports.StatisticsText(stats_text(compile_requests=1)),
            generation_failure,
        ),
        "generation_changed": (
            recorder_ports.StatisticsText(stats_text(compile_requests=1)),
            changed_generation,
        ),
        "empty_stats": (recorder_ports.StatisticsText(stats_text()), GENERATION),
        "counter_decreased": (
            recorder_ports.StatisticsText(
                stats_text(compile_requests=1, requests_executed=1, cache_hits=1)
            ),
            GENERATION,
        ),
        "ineligible_window": (
            recorder_ports.StatisticsText(
                stats_text(compile_requests=1, requests_executed=1, cache_hits=1)
            ),
            GENERATION,
        ),
        "count_mismatch": (
            recorder_ports.StatisticsText(stats_text(compile_requests=1, cache_hits=1)),
            GENERATION,
        ),
        "mixed_window": (
            recorder_ports.StatisticsText(
                stats_text(
                    compile_requests=2,
                    requests_executed=2,
                    cache_hits=1,
                    cache_misses=1,
                    compilations=1,
                )
            ),
            GENERATION,
        ),
    }
    return result_by_failure[failure]


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        ("timeout", "timeout"),
        ("read_error", "read_error"),
        ("parse_error", "parse_error"),
        ("unsupported_version", "unsupported_version"),
        ("generation_unavailable", "generation_unavailable"),
        ("generation_changed", "generation_changed"),
        ("empty_stats", "empty_stats"),
        ("counter_decreased", "counter_decreased"),
        ("ineligible_window", "ineligible_window"),
        ("count_mismatch", "count_mismatch"),
        ("mixed_window", "mixed_window"),
    ],
)
def test_each_reading_failure_is_named_and_unattributed(
    failure: str, expected_reason: str
) -> None:
    """Map every D13 failure to its reason without guessing a label."""
    recorder = outcome_module("outcome_recorder")
    ports = outcome_module("outcome_ports")
    count = 2 if failure == "mixed_window" else 1
    baseline = (
        stats_reading(compile_requests=2, requests_executed=2, cache_hits=1)
        if failure == "counter_decreased"
        else stats_reading()
    )
    payload = launch_payload(recorder, cache_key())
    state = state_with_window(
        recorder,
        WindowSetup(
            payload,
            count=count,
            clean=failure != "ineligible_window",
            baseline=baseline,
        ),
    )
    state, _ = begin_query(state, recorder, clear_gate_event(recorder, 2.0), now=2.0)
    result, after_generation = _reading_result(failure, ports)
    state, output = recorder.advance(
        state,
        recorder.PollResult(
            now=2.1,
            result=result,
            after_generation=after_generation,
        ),
    )

    assert output.events, "a failed close supplies one row per launch"
    assert all(event.cache_result == "unattributed" for event in output.events), (
        "a failed close never guesses an outcome"
    )
    assert all(event.outcome_reason == expected_reason for event in output.events), (
        "the D13 reason order and vocabulary are preserved"
    )
