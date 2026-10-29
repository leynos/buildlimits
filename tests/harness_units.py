"""User-manager unit control for the acceptance harness.

The harness runs every process it owns as a transient user service in its own
slice, so that a test can make claims about stops, restarts, readiness ordering
and descriptor limits instead of guessing from a bare process. Only the units a
harness names are ever touched, and every command runs under
:func:`manager_env`, which selects the reachable public user bus without
changing the parent manager's routing.

:func:`user_manager_available` and :func:`systemd_user_marks` live here because
they answer the same question the rest of the module acts on: whether this host
can run a unit at all. The harness module re-exports them for the suites that
ask through it.
"""

import dataclasses as dc
import os
import shutil
import socket
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed fixture argv, never a shell.
import time
import typing as typ

import pytest

from buildlimits.systemd import run

if typ.TYPE_CHECKING:
    import collections.abc as cabc
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


def wait_for(condition: cabc.Callable[[], bool], timeout: float = 15.0) -> bool:
    """Poll until a condition holds or the timeout expires.

    It lives here rather than with the harness so that the fixtures the harness
    owns can wait without importing the module that owns them.

    Parameters
    ----------
    condition : Callable[[], bool]
        The condition.
    timeout : float
        The longest wait, in seconds.

    Returns
    -------
    bool
        Whether the condition held in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()




def user_manager_available() -> bool:
    """Report whether a systemd user manager and busctl are usable.

    Returns
    -------
    bool
        Whether host acceptance was opted into and the manager is usable.
    """
    # Even collection-time readiness probes must be explicitly opted into.
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
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() in {"running", "degraded"}


def systemd_user_marks(reason: str) -> list[pytest.MarkDecorator]:
    """Mark a module as host acceptance that skips without a user manager.

    Parameters
    ----------
    reason : str
        What the module needs from the host, shown when it is skipped.

    Returns
    -------
    list[pytest.MarkDecorator]
        The ``systemd_user`` selector and the availability skip, for
        ``pytestmark``.
    """
    return [
        pytest.mark.systemd_user,
        pytest.mark.skipif(not user_manager_available(), reason=reason),
    ]


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


def stop_units(units: cabc.Iterable[str]) -> None:
    """Stop and un-fail harness-owned units, in the order given.

    Stopping is synchronous, so the caller's next command sees the state this
    left rather than one the manager has only queued. A unit that was never
    started is stopped harmlessly.

    Parameters
    ----------
    units : Iterable[str]
        The units to stop, outermost last.
    """
    for unit in units:
        for arguments, timeout in ((("stop", unit), 90), (("reset-failed", unit), 30)):
            subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - owned fixture units.
                ["/usr/bin/systemctl", "--user", *arguments],
                capture_output=True,
                check=False,
                env=manager_env(),
                timeout=timeout,
            )


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
