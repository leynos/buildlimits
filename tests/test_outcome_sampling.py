"""Child sampling cadence is opt-in, bounded and failure-aware."""

import pytest

from buildlimits.outcome_attribution import Generation
from buildlimits.outcome_ports import SampleFailure
from buildlimits.outcome_sampling import CHILD_SAMPLE_SECONDS, ChildSampler


class ScriptedServer:
    """Return deterministic sampling results and count port calls."""

    def __init__(self, *results: frozenset[str] | SampleFailure) -> None:
        """Retain ordered results for later polls."""
        self.results = list(results)
        self.calls = 0

    @staticmethod
    def generation() -> Generation:
        """Provide a stable fake identity required by the server port.

        Returns
        -------
        Generation
            A fixed test server identity.
        """
        return Generation("invocation", 123, 456)

    def full_emit_metadata(self) -> frozenset[str] | SampleFailure:
        """Return one result from the fixed script."""
        result = self.results[self.calls]
        self.calls += 1
        return result


def test_samples_only_while_cached_groups_move_at_quarter_second_cadence() -> None:
    """Idle periods own no sample and active periods do not catch up in bursts."""
    server = ScriptedServer(frozenset(), frozenset(), frozenset())
    sampler = ChildSampler(server)

    assert sampler.poll(10.0, cached_moving=0) is None, (
        "samples_only_while_cached_g...: sampler.poll(10.0, cached_moving=0) "
    )
    assert sampler.poll(10.0, cached_moving=1) == frozenset(), (
        "samples_only_while_cached_g...: sampler.poll(10.0, cached_moving=1) "
    )
    assert sampler.poll(10.249, cached_moving=1) is None, (
        "samples_only_while_cached_g...: sampler.poll(10.249, cached_moving=1"
    )
    assert sampler.poll(10.25, cached_moving=1) == frozenset(), (
        "samples_only_while_cached_g...: sampler.poll(10.25, cached_moving=1)"
    )
    assert sampler.poll(10.26, cached_moving=0) is None, (
        "samples_only_while_cached_g...: sampler.poll(10.26, cached_moving=0)"
    )
    assert sampler.poll(10.27, cached_moving=1) == frozenset(), (
        "samples_only_while_cached_g...: sampler.poll(10.27, cached_moving=1)"
    )
    assert server.calls == 3, "samples_only_while_cached_g...: server.calls == 3"


def test_failures_back_off_from_quarter_second_to_eight_seconds() -> None:
    """Repeated failures double a fixed delay and later success resets it."""
    failures: list[SampleFailure | frozenset[str]] = [
        SampleFailure("group_unreadable") for _ in range(8)
    ]
    failures.append(frozenset({"meta"}))
    server = ScriptedServer(*failures)
    sampler = ChildSampler(server)
    now = 0.0
    delays = []
    for expected in (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0):
        assert isinstance(sampler.poll(now, cached_moving=1), SampleFailure), (
            "failures_back_off_from_quar...: isinstance(sampler.poll(now, cached_"
        )
        next_due = now + expected
        assert sampler.poll(next_due - 0.001, cached_moving=1) is None, (
            "failures_back_off_from_quar...: sampler.poll(next_due - 0.001, cache"
        )
        delays.append(expected)
        now = next_due
    assert delays[-1] == pytest.approx(8.0), (
        "failures_back_off_from_quar...: delays[-1] == pytest.approx(8.0)"
    )
    assert sampler.poll(now + 8.0, cached_moving=1) == frozenset({"meta"}), (
        "failures_back_off_from_quar...: sampler.poll(now + 8.0, cached_movin"
    )
    assert (
        sampler.poll(now + 8.0 + CHILD_SAMPLE_SECONDS - 0.001, cached_moving=1) is None
    ), "the next child sample is not due yet"
