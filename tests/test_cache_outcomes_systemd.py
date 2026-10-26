"""Real-user-manager evidence for opt-in cache-outcome recording."""

import datetime as dt
import io
import json
import subprocess
from contextlib import redirect_stdout

import pytest
from cache_outcome_harness import FakeCacheConfiguration
from cache_outcome_systemd_support import OutcomeWorld, replay_report
from pytest_bdd import given, parsers, scenarios, then, when
from systemd_harness import systemd_user_marks, wait_for

from buildlimits.metrics import decode_native_fields
from buildlimits.metrics_cli import main as metrics_main
from buildlimits.metrics_query import Window
from buildlimits.metrics_records import Record
from buildlimits.outcome_record_format import OutcomeRecord

pytest_plugins = [
    "cache_outcome_systemd_support",
    "steps.cache_outcome_common_systemd_steps",
]

pytestmark = [
    *systemd_user_marks("needs a systemd user manager and busctl"),
    pytest.mark.timeout(180),
]

scenarios("features/cache_outcomes_on_systemd.feature")


@when("two cached compiles run one after the other with nothing queued")
def _two_compiles(outcome_world: OutcomeWorld) -> None:
    """Finish each build and let the recorder close its quiet-point window."""
    outcome_world.compile()
    outcome_world.wait_for_outcomes(1)
    outcome_world.compile()
    outcome_world.wait_for_outcomes(2)


@then(
    parsers.parse(
        'the journal has cache_outcome events "{first}" then "{second}" '
        'with attribution "{attribution}"'
    )
)
def _event_order(
    outcome_world: OutcomeWorld, first: str, second: str, attribution: str
) -> None:
    """Check the fixed result and attribution labels in journal order."""
    events = outcome_world.harness.read_metric_events("cache_outcome")
    assert [event["BUILD_LIMITS_CACHE_RESULT"] for event in events[:2]] == [
        first,
        second,
    ], "the recorder preserves fake cache result order"
    assert [event["BUILD_LIMITS_OUTCOME_ATTRIBUTION"] for event in events[:2]] == [
        attribution,
        attribution,
    ], "both single-group windows are attributable"


@then("the outcome store holds two records with the same key")
def _same_key(outcome_world: OutcomeWorld) -> None:
    """Assert both durable rows carry the same privacy-safe cache key."""
    records = outcome_world._records()
    keys = [record.key for record in records if isinstance(record, OutcomeRecord)]
    assert len(keys) == 2, "two outcome records reached the bounded store"
    assert keys[0] is not None and keys[0] == keys[1], "compile identity is stable"


@then("the H3 replay reports two attributed outcomes as insufficient")
def _h3_replay(outcome_world: OutcomeWorld) -> None:
    """Exercise the installed read-only H3 command on the real fixture store."""
    store_path = (
        outcome_world.harness.state_home / "buildlimits" / "cache-outcomes.jsonl"
    )
    report = replay_report("h3", store_path)
    assert report["verdict"] == "insufficient", "two rows do not meet H3's floor"
    assert report["attributed_outcomes"] == 2, "the CLI reads both stored outcomes"


@then("the metrics report agrees with the fake cache")
def _metrics_report(outcome_world: OutcomeWorld) -> None:
    """Aggregate the harness's real journal events through the report CLI."""
    events = [
        *outcome_world.harness.read_metric_events("request_admitted"),
        *outcome_world.harness.read_metric_events("cache_outcome"),
        *outcome_world.harness.read_metric_events("outcome_reading"),
        *outcome_world.harness.read_metric_events("outcome_store"),
    ]
    records = tuple(
        Record(
            "supervisor",
            event["BUILD_LIMITS_EVENT"],
            decode_native_fields(event),
            dt.datetime.now(dt.UTC),
        )
        for event in events
    )
    window = Window(records, seconds=3600, truncated=False)
    output = io.StringIO()
    with redirect_stdout(output):
        status = metrics_main(
            ["--since", "1h", "--max-records", "100000", "--json"],
            query=lambda **_options: window,
        )
    assert status == 0, "the metrics report accepts the collected event window"
    section = json.loads(output.getvalue())["cache_outcomes"]
    assert section["outcomes"] == {"hit": 1, "miss": 1}, (
        "report result counts match the scripted cache"
    )
    assert section["attributed"] == 2, "report coverage counts both outcomes"


@given("the cache recovery barrier is present")
def _barrier(outcome_world: OutcomeWorld) -> None:
    """Hold the normal quiet-point gate closed with this world's marker."""
    marker = outcome_world.harness.runtime / "cache-recovery"
    marker.touch()
    outcome_world.barrier_queries_before = outcome_world.harness.fake_cache_state()[
        "statistics_queries"
    ]


@when("a cached compile finishes")
def _compile_with_barrier(outcome_world: OutcomeWorld) -> None:
    """Complete work while recovery owns the cache barrier."""
    outcome_world.compile()


@then("no statistics query was started")
def _no_query(outcome_world: OutcomeWorld) -> None:
    """Check the barrier skip was observed without another child query."""
    assert wait_for(
        lambda: any(
            row.get("BUILD_LIMITS_GATE_REASON") == "barrier"
            for row in outcome_world.harness.read_metric_events("outcome_reading")
        ),
        timeout=5,
    ), outcome_world.harness.journal()
    assert outcome_world.harness.fake_cache_state()["statistics_queries"] == (
        outcome_world.barrier_queries_before
    ), "a barrier skip does not start the statistics client"


@given("one attributed compile in the first invocation")
def _first_invocation(outcome_world: OutcomeWorld) -> None:
    """Capture one old-generation result before restarting the cache unit."""
    outcome_world.compile()
    outcome_world.wait_for_outcomes(1)


@when("the cache unit restarts with its previous counters intact")
def _restart_cache(outcome_world: OutcomeWorld) -> None:
    """Observe the persistent counters under the fake server's new generation."""
    outcome_world.restart_cache()


@when("one cached compile runs")
def _compile_after_restart(outcome_world: OutcomeWorld) -> None:
    """Advance the cumulative counters beyond their saved pre-restart values."""
    outcome_world.compile()
    assert wait_for(
        lambda: (
            outcome_world.harness.fake_cache_state()["counters"]["compile_requests"]
            > outcome_world.old_compile_requests
        ),
        timeout=10,
    ), outcome_world.harness.journal()
    outcome_world.wait_for_outcomes(2)


@then("that compile is attributed against the new generation")
def _new_generation_result(outcome_world: OutcomeWorld) -> None:
    """Ensure the post-restart window has a fresh, usable baseline."""
    event = outcome_world.harness.read_metric_events("cache_outcome")[-1]
    assert event["BUILD_LIMITS_OUTCOME_ATTRIBUTION"] == "solo", (
        "the new generation yields a fresh single-group attribution"
    )


@given("the fake sccache answers statistics after 5 seconds")
def _delayed_statistics(outcome_world: OutcomeWorld) -> None:
    """Delay exactly the next statistics response past the query deadline."""
    outcome_world.harness.configure_fake_cache(
        FakeCacheConfiguration(
            statistics_delay_seconds=5.0,
            statistics_delay_count=1,
        )
    )


@when("a cached compile finishes and another compile is submitted")
def _compile_during_query(outcome_world: OutcomeWorld) -> None:
    """Submit a second compile while the delayed query remains outstanding."""
    before = outcome_world.harness.fake_cache_state()["statistics_queries"]
    outcome_world.compile()
    assert wait_for(
        lambda: outcome_world.harness.fake_cache_state()["statistics_queries"] > before,
        timeout=4,
    ), outcome_world.harness.journal()
    outcome_world.second_elapsed = outcome_world.compile()


@then("the second compile is admitted while the query is outstanding")
def _admitted_during_query(outcome_world: OutcomeWorld) -> None:
    """Bound the second build below the recorder's two-second query deadline."""
    assert outcome_world.second_elapsed is not None, (
        "_admitted_during_query: outcome_world.second_elapsed is not None"
    )
    assert outcome_world.second_elapsed < 1.5, (
        "the outstanding statistics query does not hold an admission token"
    )


@then(parsers.parse('an outcome_reading event reports "{reason}"'))
def _reading_failure(outcome_world: OutcomeWorld, reason: str) -> None:
    """Observe the fixed timeout reason after the child deadline expires."""
    assert wait_for(
        lambda: any(
            row.get("BUILD_LIMITS_OUTCOME_REASON") == reason
            for row in outcome_world.harness.read_metric_events("outcome_reading")
        ),
        timeout=6,
    ), outcome_world.harness.journal()


@when("a cached compile is attributed")
def _compile_for_status(outcome_world: OutcomeWorld) -> None:
    """Complete one window before reading the public status command."""
    outcome_world.compile()
    outcome_world.wait_for_outcomes(1)


@then("build-limits-status shows a cache outcomes line with 1 attributed")
def _status_line(outcome_world: OutcomeWorld) -> None:
    """Check the optional status line reflects the attributed count."""
    result = subprocess.run(
        [str(outcome_world.harness.status_command)],
        env=outcome_world.harness.env,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert "cache outcomes recording: 1 attributed" in result.stdout, (
        '_status_line: "cache outcomes recording: 1 attributed" in result.std'
    )
