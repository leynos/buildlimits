"""Pre-release launch failures preserve bounded diagnostics for requesters."""

import errno
import selectors
import typing as typ
from pathlib import Path

from request_helpers import recorded_request, refusal_from

from buildlimits.config import SupervisorConfig
from buildlimits.groups import Groups, Running
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    from buildlimits.launch import HeldChild

DIAGNOSTIC = "cannot start compiler: stage=signals errno=24 nofile=64:128"


class _FailedChild:
    """A held child that already reported a bounded setup failure."""

    errors: int | None = None

    def release(self) -> None:
        """Fail the barrier write after the child has exited."""
        raise BrokenPipeError(errno.EPIPE, "closed barrier")

    def abandon(self) -> str:
        """Return the completed diagnostic while ending child ownership."""
        return DIAGNOSTIC


def test_failed_release_preserves_child_diagnostic() -> None:
    """Release failure retains its child's setup reason after group cleanup."""
    with selectors.DefaultSelector() as selector:
        groups = Groups(SupervisorConfig(), UserManager(), selector, lambda: 0.0)
        child = typ.cast("HeldChild", _FailedChild())
        running = Running("g1", 1, Path("/unused"), child)
        groups.running["g1"] = running
        assert groups.release(running) is False, "a closed barrier cannot start work"
        assert not groups.running, "the abandoned child no longer owns a group"
        assert getattr(running, "exec_error", None) == DIAGNOSTIC, (
            "the launch caller must receive the child's bounded setup diagnostic"
        )


def test_launch_refusal_preserves_stage_and_stable_reason() -> None:
    """A stored setup error reaches the client under the exec-failed contract."""
    with recorded_request() as (_selector, client, requests, entry):
        entry.exec_error = DIAGNOSTIC
        requests.refuse(entry.request_id, "launch-failed")
        reply = refusal_from(client)
        assert reply.reason == "exec-failed", "the existing reason code remains stable"
        assert reply.message == DIAGNOSTIC, "bounded stage context is preserved"
