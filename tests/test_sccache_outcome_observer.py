"""Controlled outcome observation against the real sccache 0.14 backend.

The unit recorder contracts use fake ports. This opt-in check drives the same
reducer through the real statistics and generation ports, so the counter
footprints, labels and admission times are evidence from the supported binary
rather than from a fixture's model of it.
"""

import datetime as dt
import typing as typ
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

if typ.TYPE_CHECKING:
    from sccache_outcome_observer_support import Toolchain

    from buildlimits.outcome_attribution import Generation
    from buildlimits.outcome_ports import GenerationFailure
    from buildlimits.outcome_record_format import StoreRecord

pytestmark = [
    *sccache_host_marks(),
    pytest.mark.timeout(180),
]


def _recorder(
    tools: Toolchain, socket_path: Path
) -> tuple[OutcomeRecorder, list[Generation | GenerationFailure | None]]:
    """Build one driver over the real statistics port and a switchable server.

    The driver reads the wall clock, so each row it emits is dated at the
    reading that produced it, the way the supervisor's shell dates its own.

    Returns
    -------
    tuple[OutcomeRecorder, list[object]]
        The driver and the server's one-element generation slot.
    """
    server, slot = switchable_server(socket_path)
    statistics = SccacheStatistics(tools.executable, tools.environment, socket_path)
    return OutcomeRecorder(
        statistics, server, wall_clock=lambda: dt.datetime.now(dt.UTC)
    ), slot


def test_the_real_sccache_observer_labels_controlled_windows(tmp_path: Path) -> None:
    """Miss, hit, mixed, declined and timeout are told apart exactly."""
    tools = toolchain(tmp_path)
    records: list[StoreRecord] = []

    with _private_server(
        tools.executable, tools.environment, tmp_path / "server.log"
    ) as (_server, _scoped):
        driver, _slot = _recorder(tools, tmp_path / "s.sock")
        baseline = read(driver)
        assert baseline.reason == "none", "the fresh generation is the baseline"
        assert baseline.events == (), "an empty baseline emits no label"

        missed = single_compile(
            driver,
            tools,
            metadata="observer-cold",
            group_id="cold",
        )
        miss_record = assert_cold_miss(missed)
        records.extend(missed.records)

        hit = single_compile(
            driver,
            tools,
            metadata="observer-cold",
            group_id="warm",
        )
        assert_warm_hit(hit, miss_record.key)
        records.extend(hit.records)

        mixed = run_mixed(driver, tools)
        assert_mixed(mixed)
        records.extend(mixed.records)

        refused = run_declined(driver, tools)
        (declined_record,) = outcomes(refused.records)
        assert declined_record.outcome == "declined", "the server declined the probe"
        assert declined_record.outcome_attribution == "solo", "one clean launch"
        records.extend(refused.records)

        timed_out = run_timeout(driver, tools)
        assert timed_out.reading_reasons == ("timeout",), "the deadline is enforced"
        (timeout_record,) = outcomes(timed_out.records)
        assert timeout_record.outcome == "unattributed", "no label on a timeout"
        assert timeout_record.outcome_reason == "timeout", "the reason is explicit"
        records.extend(timed_out.records)

        recovered = read(driver)
        assert recovered.reason == "none", "the timeout leaves a fresh baseline"

    assert_extraction(records)


def test_the_real_sccache_observer_refuses_a_restart_window(tmp_path: Path) -> None:
    """A generation change inside a query leaves its launch unlabelled."""
    tools = toolchain(tmp_path)

    with _private_server(
        tools.executable, tools.environment, tmp_path / "server.log"
    ) as (_server, _scoped):
        driver, slot = _recorder(tools, tmp_path / "s.sock")
        assert read(driver).reason == "none", "the baseline is accepted"
        restarted = run_restart(driver, slot, tools)

    assert restarted.reason == "generation_changed", (
        "a server restart inside the query is refused"
    )
    (record,) = outcomes(restarted.records)
    assert record.outcome == "unattributed", "no label across a restart"
    assert record.outcome_reason == "generation_changed", (
        "the restart reason is explicit"
    )
    assert record.admitted_at is not None, "the refused row still carries admission"
