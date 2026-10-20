"""Health failures preserve results from already completed compiler leaders."""

import typing as typ

import pytest
from request_helpers import crate_pending

from buildlimits.admission import GroupState
from buildlimits.effects import Exit

if typ.TYPE_CHECKING:
    from conftest import RecordingSink

    from buildlimits.supervisor import Supervisor


def test_health_failure_preserves_successful_draining_result(
    observed_supervisor: Supervisor, metric_sink: RecordingSink
) -> None:
    """A successful leader stays completed when health fails during draining."""
    observed_supervisor.admission.submit(1)
    observed_supervisor.admission.token_arrived(b"a")
    entry = crate_pending(1, "g1", admitted=1.0)
    observed_supervisor.requests.pending[1] = entry
    observed_supervisor._apply(
        observed_supervisor.admission.leader_exited("g1", Exit(code=0))
    )
    assert observed_supervisor.admission.groups["g1"].state is GroupState.DRAINING, (
        "the successful leader must still be awaiting group emptiness"
    )

    observed_supervisor._unhealthy("device replaced")
    assert entry.cancellation == "none", "health must not cancel a finished leader"
    observed_supervisor._apply(observed_supervisor.admission.group_emptied("g1"))

    finished = metric_sink.of_event("request_finished")
    assert len(finished) == 1, "group emptiness must emit exactly one result"
    assert finished[0]["BUILD_LIMITS_OUTCOME"] == "completed", (
        "health failure must preserve the successful leader's result"
    )
    assert finished[0]["BUILD_LIMITS_CANCELLATION"] == "none", (
        "draining after success is not a health cancellation"
    )
    assert not observed_supervisor.requests.pending, (
        "completion must forget the request"
    )


@pytest.mark.parametrize("running", [False, True], ids=["queued", "running"])
@pytest.mark.parametrize("cause", ["none", "disconnect"])
def test_health_failure_marks_only_uncancelled_active_requests(
    observed_supervisor: Supervisor, running: bool, cause: str
) -> None:
    """Queued and running requests retain their first cancellation cause."""
    observed_supervisor.admission.submit(1)
    if running:
        observed_supervisor.admission.token_arrived(b"a")
    entry = crate_pending(1, "g1", admitted=1.0) if running else crate_pending(1)
    entry.cancellation = cause
    observed_supervisor.requests.pending[1] = entry

    observed_supervisor._unhealthy("device replaced")

    assert entry.cancellation == ("health" if cause == "none" else cause), (
        "health marks active requests without overwriting an earlier cause"
    )
