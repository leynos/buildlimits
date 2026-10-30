"""Tests for the human-readable ``build-limits-status`` summary.

Each fixture is a fixed status document, so its request identifiers and
elapsed times are stable; directories are a ``<project>`` placeholder rather
than an absolute path. The semantic assertions pin the line each variant is
about; the snapshot pins the whole summary an operator reads.
"""

import typing as typ

import pytest
from status_render_states import STATES

from buildlimits.status_render import render

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


@pytest.mark.parametrize("name", STATES)
def test_the_summary_describes_each_state(
    name: str, snapshot: SnapshotAssertion
) -> None:
    """Every health, cache, queue and group variant renders as expected."""
    state, expected_line = STATES[name]
    rendered = render(state)
    assert expected_line in rendered, rendered
    assert len(rendered.splitlines()) >= 3, "summary, queue and group sections"
    assert rendered == snapshot, "the rendered summary changed"


def test_a_live_work_fault_on_a_healthy_supervisor_reports_it_healthy() -> None:
    """A retained drain is not a health failure, so the summary says healthy.

    This is the reading the users' guide shows for a retained managed cache:
    the fault and its guidance render below a summary that stays healthy.
    """
    state, _line = STATES["live-work-guidance"]
    lines = render({**state, "healthy": True}).splitlines()
    assert lines[0] == "capacity 4, held 1, healthy, caching through sccache", (
        "the summary keeps the supervisor's own health"
    )
    assert "drain fault kind: live_work" in lines, "the fault is still reported"


def test_the_outcome_line_names_the_last_closed_reason() -> None:
    """The G1 runbook asks for the last reason without a second command."""
    state, line = STATES["outcomes-enabled"]
    assert line in render(state).splitlines(), "the reason rides on the line"

    legacy = {
        **state,
        "cache_outcomes": {"enabled": True, "attributed": 1, "readings": 2},
    }
    assert "last reason" not in render(legacy), (
        "a document without a closed reason is unchanged"
    )


def test_blocked_admission_precedes_the_healthy_headline() -> None:
    """A healthy process headline cannot hide blocked admission."""
    state, _line = STATES["blocked-admission"]
    lines = render(state).splitlines()
    assert lines[0].startswith("admission blocked:"), "blocking is the first line"
    assert lines[0] != "capacity 4, held 4, healthy, caching through sccache", (
        "the first line does not imply usable admission"
    )
    assert lines[1] == "capacity 4, held 4, healthy, caching through sccache", (
        "process health remains visible below admission state"
    )


def test_ready_saturated_and_legacy_rendering_remains_byte_identical() -> None:
    """Compatible documents keep their established human-readable output."""
    base = {"capacity": 4, "held": 0, "healthy": True, "caching": True}
    original = render(base)
    assert (
        render({**base, "admission": {"state": "ready", "ready": True}}) == original
    ), "ready admission adds no line"
    assert (
        render({**base, "admission": {"state": "saturated", "ready": True}}) == original
    ), "saturated admission adds no line"
    assert render({**base, "admission": None}) == original, "missing data is ignored"
    assert render({**base, "admission": "legacy"}) == original, (
        "non-dictionary legacy data is ignored"
    )
