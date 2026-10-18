"""Generated invocation histories preserve terminal and admission boundaries."""

import pytest
from conftest import RecordingSink
from hypothesis import example, given
from hypothesis import strategies as st

from buildlimits.adapter_metrics import AdapterMetrics
from buildlimits.metrics import MAX_COUNTER, Metrics


def _expected_record(
    history: list[str], initial_attempts: int, step: int
) -> dict[str, str]:
    """Derive a summary from the immutable prefix ending at the first exit."""
    terminal = next(
        index
        for index, action in enumerate(history)
        if action in {"finish", "interrupt"}
    )
    prefix = history[:terminal]
    admissions = [index for index, action in enumerate(prefix) if action == "admit"]
    wait = (admissions[0] + 1) * step if admissions else (terminal + 1) * step
    runtime = (terminal - admissions[0]) * step if admissions else 0
    return {
        "BUILD_LIMITS_SCHEMA": "1",
        "BUILD_LIMITS_SOURCE": "adapter",
        "BUILD_LIMITS_EVENT": "adapter_finished",
        "BUILD_LIMITS_OUTCOME": "interrupted"
        if history[terminal] == "interrupt"
        else "completed",
        "BUILD_LIMITS_CONNECTION_ATTEMPTS": str(
            min(MAX_COUNTER, initial_attempts + prefix.count("attempt"))
        ),
        "BUILD_LIMITS_BUSY_RETRIES": "0",
        "BUILD_LIMITS_RESTART_RETRIES": "0",
        "BUILD_LIMITS_WAIT_SECONDS": f"{wait:.3f}",
        "BUILD_LIMITS_RUNTIME_SECONDS": f"{runtime:.3f}",
    }


@given(
    actions=st.lists(
        st.sampled_from(["attempt", "admit", "finish", "interrupt"]), max_size=40
    ),
    initial_attempts=st.sampled_from([0, MAX_COUNTER - 1, MAX_COUNTER]),
    step=st.integers(min_value=0, max_value=1000),
)
@example(
    actions=["admit", "admit", "finish", "interrupt", "finish"],
    initial_attempts=0,
    step=1,
)
@example(
    actions=["attempt", "attempt", "finish", "finish"],
    initial_attempts=MAX_COUNTER - 1,
    step=0,
)
def test_invocation_history(
    actions: list[str], initial_attempts: int, step: int
) -> None:
    """Only the first terminal boundary emits, using the first admission and capped retries."""
    sink = RecordingSink()
    now = [10.0]
    summary = AdapterMetrics(lambda: now[0], now[0], Metrics(sink, source="adapter"))
    summary.attempts = initial_attempts
    summary.outcome = "completed"
    history = [*actions, "finish"]
    for index, action in enumerate(history):
        now[0] = 10.0 + (index + 1) * step
        match action:
            case "attempt":
                summary.attempted()
            case "admit":
                summary.admission(now[0])
            case "interrupt":
                summary.interrupted()
            case "finish":
                summary.finish()
    assert len(sink.records) == 1, "at most one terminal record across repeated exits"
    assert sink.records[0] == _expected_record(history, initial_attempts, step), (
        "the first terminal prefix alone determines the summary"
    )
    assert summary.attempts == min(
        MAX_COUNTER, initial_attempts + history.count("attempt")
    ), "all retry increments saturate, including those after emission"
    all_admissions = [
        index for index, action in enumerate(history) if action == "admit"
    ]
    if all_admissions:
        assert summary.admitted == pytest.approx(
            10.0 + (all_admissions[0] + 1) * step
        ), "the first admission always wins"
    else:
        assert summary.admitted is None, "no admission was observed"
