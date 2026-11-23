"""Controlled outcome observation against the real sccache 0.14 backend.

The unit recorder contracts use fake ports. This opt-in check drives the same
reducer through the real statistics and generation ports, so the counter
footprints, labels and admission times are evidence from the supported binary
rather than from a fixture's model of it.
"""

from pathlib import Path

import pytest
from sccache_host import _private_server, sccache_host_marks
from sccache_outcome_observer_support import (
    assert_cold_miss,
    assert_extraction,
    assert_mixed,
    assert_warm_hit,
    outcomes,
    read,
    run_declined,
    run_mixed,
    run_restart,
    run_timeout,
    single_compile,
    switchable_server,
    toolchain,
)

from buildlimits.outcome_queries import SccacheStatistics
from buildlimits.outcome_recorder_driver import OutcomeRecorder

pytestmark = [
    *sccache_host_marks(),
    pytest.mark.timeout(180),
]


def test_the_real_sccache_observer_labels_controlled_windows(tmp_path: Path) -> None:
    """Miss, hit, mixed, declined and timeout are told apart exactly."""
    tools = toolchain(tmp_path)
    records: list[object] = []

    with _private_server(
        tools.executable, tools.environment, tmp_path / "server.log"
    ) as (_server, _scoped):
        server, _slot = switchable_server(tmp_path / "s.sock")
        recorder = OutcomeRecorder(
            SccacheStatistics(tools.executable, tools.environment, tmp_path / "s.sock"),
            server,
        )
        baseline = read(recorder)
        assert baseline.reason == "none", "the fresh generation is the baseline"
        assert baseline.events == (), "an empty baseline emits no label"

        missed = single_compile(
            recorder,
            tools,
            metadata="observer-cold",
            group_id="cold",
        )
        miss_record = assert_cold_miss(missed)
        records.extend(missed.records)

        hit = single_compile(
            recorder,
            tools,
            metadata="observer-cold",
            group_id="warm",
        )
        assert_warm_hit(hit, miss_record.key)
        records.extend(hit.records)

        mixed = run_mixed(recorder, tools)
        assert_mixed(mixed)
        records.extend(mixed.records)

        refused = run_declined(recorder, tools)
        (declined_record,) = outcomes(refused.records)
        assert declined_record.outcome == "declined", "the server declined the probe"
        assert declined_record.outcome_attribution == "solo", "one clean launch"
        records.extend(refused.records)

        timed_out = run_timeout(recorder, tools)
        assert timed_out.reading_reasons == ("timeout",), "the deadline is enforced"
        (timeout_record,) = outcomes(timed_out.records)
        assert timeout_record.outcome == "unattributed", "no label on a timeout"
        assert timeout_record.outcome_reason == "timeout", "the reason is explicit"
        records.extend(timed_out.records)

        recovered = read(recorder)
        assert recovered.reason == "none", "the timeout leaves a fresh baseline"

    assert_extraction(records)


def test_the_real_sccache_observer_refuses_a_restart_window(tmp_path: Path) -> None:
    """A generation change inside a query leaves its launch unlabelled."""
    tools = toolchain(tmp_path)

    with _private_server(
        tools.executable, tools.environment, tmp_path / "server.log"
    ) as (_server, _scoped):
        server, slot = switchable_server(tmp_path / "s.sock")
        recorder = OutcomeRecorder(
            SccacheStatistics(tools.executable, tools.environment, tmp_path / "s.sock"),
            server,
        )
        assert read(recorder).reason == "none", "the baseline is accepted"
        restarted = run_restart(recorder, slot, tools)

    assert restarted.reason == "generation_changed", (
        "a server restart inside the query is refused"
    )
    (record,) = outcomes(restarted.records)
    assert record.outcome == "unattributed", "no label across a restart"
    assert record.outcome_reason == "generation_changed", (
        "the restart reason is explicit"
    )
    assert record.admitted_at is not None, "the refused row still carries admission"
