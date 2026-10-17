"""Supervisor fixtures release selectors without relying on delayed collection."""

import gc

import pytest
from descriptor_helpers import isolated, open_fds
from test_supervisor_startup import (
    test_an_unknown_server_load_keeps_the_pool_closed as _startup_fixture,
)
from test_supervisor_stop import (
    test_a_stop_request_waits_for_the_event_loop as _stop_fixture,
)


def _fixture_descriptors(_report: int) -> object:
    """Exercise both actual fixtures with automatic collection disabled."""
    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    try:
        baseline = open_fds()
        with pytest.MonkeyPatch.context() as monkeypatch:
            _startup_fixture(monkeypatch)
        after_startup = open_fds()
        _stop_fixture()
        return {
            "startup": sorted(after_startup - baseline),
            "stop": sorted(open_fds() - after_startup),
            "baseline": open_fds() == baseline,
        }
    finally:
        if enabled:
            gc.enable()


def test_supervisor_fixtures_close_selectors_before_collection() -> None:
    """Discarding either fixture cannot leave a selector for later tests to collect."""
    result = isolated(_fixture_descriptors)
    assert isinstance(result, dict), "the disposable child reports fixture ownership"
    assert not result["startup"], "the rejected-start fixture closes its selector"
    assert not result["stop"], "the stopped-loop fixture closes its selector"
    assert result["baseline"], "both fixtures return to the exact descriptor baseline"
