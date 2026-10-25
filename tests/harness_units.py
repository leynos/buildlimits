"""User-manager unit control for the acceptance harness.

The harness runs every process it owns as a transient user service in its own
slice, so that a test can make claims about stops, restarts, readiness ordering
and descriptor limits instead of guessing from a bare process. Only the units a
harness names are ever touched, and every command runs under
:func:`manager_env`, which selects the reachable public user bus without
changing the parent manager's routing.
"""

import dataclasses as dc
import os
import shutil
import socket
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed fixture argv, never a shell.
import typing as typ

import pytest

from buildlimits.systemd import run

if typ.TYPE_CHECKING:
    from pathlib import Path


@dc.dataclass(frozen=True, slots=True)
class Placement:
    """Where a harness's transient unit runs and how far it may open files.

    Parameters
    ----------
    parent_slice : str
        The slice that confines every unit the owning harness creates.
    nofile : tuple[int, int] | None
        Optional soft/hard descriptor limit for the unit.
    """

    parent_slice: str
    nofile: tuple[int, int] | None = None


def manager_env() -> dict[str, str]:
    """Select the reachable public user bus for harness-owned manager commands.

    Returns
    -------
    dict[str, str]
        Command-local environment leaving the parent manager routing unchanged.
    """
    env = os.environ.copy()
    env.pop("XDG_RUNTIME_DIR", None)
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
    return env


def user_manager_available() -> bool:
    """Report whether opted-in systemd user-manager tests can run.

    Returns
    -------
    bool
        Whether host acceptance was opted into and the manager is usable.
    """
    if os.environ.get("BUILD_LIMITS_HOST_ACCEPTANCE") != "1":
        return False
    if not shutil.which("busctl") or not shutil.which("systemd-run"):
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
            check=False,
            env=manager_env(),
            timeout=30,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return result.stdout.strip() in {"running", "degraded"}


def systemd_user_marks(reason: str) -> list[pytest.MarkDecorator]:
    """Mark host acceptance tests to skip without an opted-in user manager.

    Parameters
    ----------
    reason : str
        What the host acceptance test requires, shown when it is skipped.

    Returns
    -------
    list[pytest.MarkDecorator]
        The systemd selector and availability skip for ``pytestmark``.
    """
    return [
        pytest.mark.systemd_user,
        pytest.mark.skipif(not user_manager_available(), reason=reason),
    ]


def unit_properties(unit: str, *names: str) -> dict[str, str]:
    """Read named properties of a user unit over the public user bus.

    Parameters
    ----------
    unit : str
        The unit to query.
    *names : str
        The properties to read.

    Returns
    -------
    dict[str, str]
        Each requested property's value, keyed by name.
    """
    argv = ["/usr/bin/systemctl", "--user", "show", unit]
    for name in names:
        argv.extend(("-p", name))
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - absolute systemctl, fixed argv.
        argv,
        env=manager_env(),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def run_unit(unit: str, argv: list[str], env: dict[str, str], place: Placement) -> None:
    """Start a transient user service inside a harness's own slice.

    Parameters
    ----------
    unit : str
        Name of the unit to create.
    argv : list[str]
        The service's executable and arguments.
    env : dict[str, str]
        Environment entries to set for the service.
    place : Placement
        The slice and optional descriptor limit for the unit.
    """
    command = [
        "systemd-run",
        "--user",
        "--quiet",
        f"--unit={unit}",
        f"--slice={place.parent_slice}",
    ]
    if place.nofile is not None:
        command.append(f"--property=LimitNOFILE={place.nofile[0]}:{place.nofile[1]}")
    for key, value in env.items():
        command.append(f"--setenv={key}={value}")
    command.append("--setenv=PYTHONPATH=" + os.environ.get("PYTHONPATH", ""))
    subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - owned fixture unit, fixed argv.
        [*command, *argv],
        check=True,
        env=manager_env(),
        timeout=30,
    )


def listening(runtime: Path) -> bool:
    """Report whether a supervisor accepts connections in its runtime.

    Parameters
    ----------
    runtime : Path
        The supervisor's private runtime directory.

    Returns
    -------
    bool
        Whether a connection succeeded.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        try:
            probe.connect(str(runtime / "supervisor.sock"))
        except OSError:
            return False
    return True


def unit_active(unit: str) -> bool:
    """Report whether a unit is active.

    Parameters
    ----------
    unit : str
        The unit to query.

    Returns
    -------
    bool
        Whether systemd reports the unit active.
    """
    result = run(
        [
            "/usr/bin/systemctl",
            "--user",
            "is-active",
            "--quiet",
            unit,
        ],
    )
    return result.returncode == 0
