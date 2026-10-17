"""A failed admission acknowledgement must leave the compiler unreleased."""

import errno
import typing as typ
from pathlib import Path

import pytest
from descriptor_helpers import open_fds
from pool_helpers import attach_pool
from request_helpers import compile_request, recorded_request

from buildlimits import launch
from buildlimits.config import SupervisorConfig
from buildlimits.groups import Groups, Running
from buildlimits.supervisor import Supervisor

if typ.TYPE_CHECKING:
    from buildlimits.launch import HeldChild
    from buildlimits.requesters import Pending


class _Held:
    """Track a placed launch without creating a real compiler process."""

    errors: int | None = None
    released = False
    abandoned = False
    diagnostic: str | None = None

    def release(self) -> None:
        """Record any unsafe release after the requester has disconnected."""
        self.released = True

    def abandon(self) -> str | None:
        """Record abandonment before the token can be returned."""
        self.abandoned = True
        return self.diagnostic


@pytest.mark.parametrize("early_failure", [False, True])
def test_admission_reply_failure_abandons_before_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    early_failure: bool,
) -> None:
    """Disconnected acknowledgement returns exactly one token without executing."""
    import socket

    baseline = open_fds()
    supervisor = Supervisor(SupervisorConfig(cache_mode="direct"))
    pool = attach_pool(supervisor)
    child = _Held()
    if early_failure:
        child.diagnostic = "cannot start true: stage=signals errno=24 nofile=64:128"
    client, server = socket.socketpair()
    with client, server:
        try:
            entry = supervisor.requests._admit(server, compile_request(), [])
            assert entry is not None, "the request is validated before token admission"
            supervisor._apply(supervisor.admission.submit(entry.request_id))
            client.close()

            def place(
                _groups: Groups, pending: Pending, group_id: str, *, cached: bool
            ) -> Running:
                """Install an owned held child without starting a process."""
                del cached
                running = Running(
                    group_id, pending.request_id, tmp_path, typ.cast("HeldChild", child)
                )
                _groups.running[group_id] = running
                return running

            monkeypatch.setattr(Groups, "place", place)
            monkeypatch.setattr(Groups, "cache_notice", lambda *_args, **_kwargs: None)
            supervisor._apply(supervisor.admission.token_arrived(b"a"))
            assert not child.released, (
                "an acknowledgement failure cannot run the compiler"
            )
            assert child.abandoned, (
                "held-child ownership ends before returning its token"
            )
            assert entry.exec_error == child.diagnostic, (
                "early setup diagnostics survive abandonment"
            )
            if early_failure:
                assert child.diagnostic is not None, (
                    "the injected failure has a diagnostic"
                )
                assert child.diagnostic in capsys.readouterr().err, (
                    "a disconnected requester leaves bounded journal evidence"
                )
            assert pool.returned == [b"a"], (
                "the admitted token is returned exactly once"
            )
            assert supervisor.admission.tokens_held() == 0, (
                "no abandoned group remains admitted"
            )
            assert not supervisor.groups.running and not supervisor.requests.pending, (
                "all group and requester ownership is cleared"
            )
        finally:
            for request_id in list(supervisor.requests.pending):
                supervisor.requests.forget(request_id)
            supervisor.groups.running.clear()
            supervisor.selector.close()
    assert open_fds() == baseline, (
        "all real socket and selector descriptors return to baseline"
    )


def test_parent_spawn_failure_names_bounded_program(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pre-fork exhaustion retains the executable basename and setup stage."""
    from buildlimits.systemd import UserManager

    with recorded_request() as (selector, _client, _requests, entry):
        groups = Groups(
            SupervisorConfig(cache_mode="direct"), UserManager(), selector, lambda: 0.0
        )
        groups.slice_dir = tmp_path

        def fail(_spec: launch.LaunchSpec) -> HeldChild:
            """Inject descriptor exhaustion before creating any child."""
            raise OSError(errno.EMFILE, "injected pressure")

        monkeypatch.setattr(launch, "spawn_held", fail)
        assert groups.place(entry, "g1", cached=False) is None, (
            "failed spawn creates no compiler group"
        )
        assert entry.exec_error is not None, "the request carries bounded setup context"
        assert entry.exec_error.startswith("cannot start true: stage=spawn errno=24"), (
            "parent-side setup names the executable and failing stage"
        )
        assert not groups.running, "failed setup retains no group"
