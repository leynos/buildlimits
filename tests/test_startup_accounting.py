"""Admission startup cannot infer completed cache work from an idle live server."""

import subprocess
import typing as typ
from pathlib import PurePosixPath

import pytest

from buildlimits import startup
from buildlimits import supervisor as supervisor_module
from buildlimits.config import SupervisorConfig
from buildlimits.sccache import SccacheError
from buildlimits.supervisor import Supervisor
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


@pytest.mark.parametrize("state", ["active", "activating", "deactivating"])
def test_live_cache_startup_refuses_before_pool_or_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """A potentially detached request remains covered before any new admission."""
    opened: list[Path] = []

    def runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Return a live or transitioning cache with no measured compiler children."""
        return subprocess.CompletedProcess(
            argv, 0, f"MainPID=4242\nControlGroup=\nActiveState={state}\n", ""
        )

    def pool(path: Path) -> object:
        """Record an unsafe attempt to open admission before previous accounting."""
        opened.append(path)
        msg = "pool must not open before cache accounting"
        raise AssertionError(msg)

    monkeypatch.setattr(startup, "drain_slice", lambda *_args: 0)
    monkeypatch.setattr(supervisor_module, "TokenPool", pool)
    settings = SupervisorConfig(socket_path=tmp_path / "run" / "supervisor.sock")
    instance = Supervisor(settings, UserManager(runner))
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
