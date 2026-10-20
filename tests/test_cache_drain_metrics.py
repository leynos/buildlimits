"""Due cache probes distinguish ordinary live work from drain faults."""

import typing as typ

import pytest

from buildlimits.admission import Group, GroupState, ReturnToken
from buildlimits.sccache import LiveCacheWorkError, SccacheError

if typ.TYPE_CHECKING:
    from conftest import RecordingSink

    from buildlimits.sccache import ManagedSccache
    from buildlimits.supervisor import Supervisor


@pytest.fixture
def supervisor(observed_supervisor: Supervisor) -> Supervisor:
    """Add one cached group whose token requires accounted drain proof."""
    observed_supervisor.admission.groups["g1"] = Group(
        "g1", 1, b"a", True, state=GroupState.SERVER_DRAIN
    )
    return observed_supervisor


class ScriptedCache:
    """A cache probe with explicit results and an exact operation count."""

    def __init__(self, outcomes: list[int | Exception]) -> None:
        """Keep the probe script without acquiring any cache resources."""
        self.outcomes = outcomes
        self.calls = 0

    def accounted_drain(self) -> int:
        """Return a scripted accounted value or raise the original probe fault."""
        self.calls += 1
        value = self.outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _progress(sink: RecordingSink) -> list[dict[str, str]]:
    """Read only bounded drain records from the complete event stream."""
    return sink.of_event("drain_progress")


@pytest.mark.parametrize(
    "case",
    [
        (LiveCacheWorkError("secret cache remains live"), "pending", "completed", "0"),
        (SccacheError("secret manager query"), "failed", "recovered", "1"),
    ],
)
def test_due_cache_probe_counts_only_real_attempts_and_successful_recovery(
    supervisor: Supervisor,
    metric_sink: RecordingSink,
    case: tuple[SccacheError, str, str, str],
) -> None:
    """Live cached work is pending while unknown accounting remains a fault."""
    error, state, recovered, failures = case
    cache = ScriptedCache([error, 0])
    supervisor.server.server = typ.cast("ManagedSccache", cache)
    assert supervisor.server.drained_tokens() == [], "unaccounted work retains tokens"
    assert supervisor.admission.tokens_held() == 1, "a probe fault never reclaims work"
    first = _progress(metric_sink)[0]
    assert first["BUILD_LIMITS_DRAIN_STATE"] == state, "the fault category is fixed"
    assert first["BUILD_LIMITS_DRAIN_FAILURES"] == failures, (
        "ordinary live work never increments the actual failure counter"
    )
    supervisor.server._next_drain = 4.0
    assert supervisor.server.drained_tokens() == [], (
        "before-deadline probes are skipped"
    )
    assert cache.calls == 1, "a skipped probe cannot manufacture an attempt"
    supervisor.server._next_drain = 3.0
    assert supervisor.server.drained_tokens() == [ReturnToken(b"a")], (
        "only verified stopped-empty accounting releases the token"
    )
    records = _progress(metric_sink)
    assert records[-1]["BUILD_LIMITS_DRAIN_STATE"] == recovered, (
        "only an actual non-pending success can report recovery"
    )
    assert cache.calls == len(records) == 2, "one observation per actual cache probe"
    assert "secret" not in str(records), "cache fault messages never become labels"


def test_no_wait_reset_never_claims_cache_recovery(
    supervisor: Supervisor, metric_sink: RecordingSink
) -> None:
    """Clearing drain bookkeeping without a probe emits no recovery observation."""
    cache = ScriptedCache([SccacheError("unavailable"), 0])
    supervisor.server.server = typ.cast("ManagedSccache", cache)
    supervisor.server.drained_tokens()
    recorded = tuple(metric_sink.records)
    supervisor.admission.groups.clear()
    supervisor.server.drained_tokens()
    assert tuple(metric_sink.records) == recorded, (
        "no waiting work emits no probe event"
    )
    assert cache.calls == 1, "a no-wait reset never queries cache state"
    supervisor.admission.groups["g2"] = Group(
        "g2", 2, b"b", True, state=GroupState.SERVER_DRAIN
    )
    supervisor.server.drained_tokens()
    assert _progress(metric_sink)[-1]["BUILD_LIMITS_DRAIN_STATE"] == "recovered", (
        "only the later actual stopped-empty query clears the observation fault"
    )
