"""Admission startup cannot infer completed cache work from an idle live server."""

import typing as typ
from pathlib import PurePosixPath

import pytest
from cache_unit_fake import CacheUnit, FakeClock

from buildlimits import startup
from buildlimits import supervisor as supervisor_module
from buildlimits.config import SupervisorConfig
from buildlimits.sccache import SccacheError
from buildlimits.supervisor import Supervisor
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("state", ["active", "activating", "deactivating"])
def test_a_cache_that_will_not_stop_refuses_before_pool_or_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """A live cache that start-up cannot prove stopped keeps admission closed.

    Start-up stops a live cache itself, so the refusal that remains is the one
    where the stop is accepted but the unit never leaves its live state: the
    potentially detached request is then still uncovered, and no pool opens.
    """
    opened: list[Path] = []
    unit = CacheUnit(
        barrier=tmp_path / "run" / "cache-recovery",
        state=f"MainPID=4242\nControlGroup=\nActiveState={state}\n",
        never_stops=True,
    )

    def pool(path: Path) -> object:
        """Record an unsafe attempt to open admission before previous accounting."""
        opened.append(path)
        msg = "pool must not open before cache accounting"
        raise AssertionError(msg)

    clock = FakeClock()
    monkeypatch.setattr(startup, "drain_slice", lambda *_args: 0)
    monkeypatch.setattr(supervisor_module, "TokenPool", pool)
    settings = SupervisorConfig(
        socket_path=tmp_path / "run" / "supervisor.sock",
        recovery_marker=unit.barrier,
    )
    instance = Supervisor(settings, UserManager(unit.runner), clock.timing())
    try:
        with pytest.raises(SccacheError, match="operator action"):
            instance.start(
                PurePosixPath(
                    "/user.slice/user-1000.slice/user@1000.service/buildlimits.slice/supervisor.service"
                )
            )
        assert not opened, "previous unknown work prevents pool ownership"
        assert instance.listener is None, "no new request can enter admission"
        assert not instance.admission.groups, "refusal creates no compiler token"
        assert unit.jobs == ["stop"], "the cache was asked to stop and never restored"
        assert unit.barrier.exists(), "an unaccounted cache stays behind the barrier"
    finally:
        instance.selector.close()


@pytest.mark.parametrize("sticky", [True, False])
def test_service_distinguishes_operator_accounting_from_retryable_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sticky: bool
) -> None:
    """Unknown previous cache work exits 2; transient startup errors exit 1."""
    from buildlimits import service
    from buildlimits.sccache import StartupAccountingError

    class FailedSupervisor:
        """Expose only the startup boundary; no service or pool is opened."""

        def __init__(self, _settings: SupervisorConfig, *, metrics: object) -> None:
            """Keep this fault fixture independent of real admission resources."""

        def start(self, _own: PurePosixPath) -> None:
            """Raise the selected deterministic startup failure."""
            if sticky:
                msg = "operator accounting required"
                raise StartupAccountingError(msg)
            msg = "temporary manager fault"
            raise SccacheError(msg)

    monkeypatch.setattr(service, "Supervisor", FailedSupervisor)
    monkeypatch.setattr(service.cgroups, "own_path", lambda: PurePosixPath("/fixture"))
    settings = SupervisorConfig(socket_path=tmp_path / "supervisor.sock")
    assert service._supervise(settings) == (2 if sticky else 1), (
        "only the unsafe accounting case disables automatic restart"
    )
