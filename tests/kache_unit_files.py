"""A separately managed kache daemon the harness starts in its own unit.

ADR 005 requires an admitted group to be pointed at a daemon that is already
answering, because a daemon started from inside a group would stay in that
group's scope and hold its token. The fixture therefore starts the daemon
*before* the supervisor, as an unrelated unit in the harness's own slice, and
gives it the socket the supervisor and the groups are configured with. Nothing
here is owned by the supervisor: the harness starts the daemon and the harness
stops it.

It is deliberately not a ``Wants=`` dependency of the supervisor, unlike the
managed sccache unit, because making admission depend on it would model an
ownership the supervisor must never have.
"""

import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed fixture argv, never a shell.
import sys
from pathlib import Path

from harness_units import manager_env, wait_for

FIXTURES = Path(__file__).with_name("fixtures")


def start_kache_daemon(unit: str, socket: Path, parent_slice: str) -> None:
    """Start the fake kache daemon as a unit this harness alone controls.

    Parameters
    ----------
    unit : str
        The transient unit to create for the daemon.
    socket : Path
        The socket the daemon binds and every group is pointed at.
    parent_slice : str
        The harness's slice, so that cleanup stops the daemon with its owner.

    Raises
    ------
    RuntimeError
        If the unit does not stay active or the socket never answers. A
        supervisor started against it would otherwise compose every group
        directly, and the scenario would pass while proving nothing.
    """
    subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - owned fixture unit, fixed argv.
        [
            "/usr/bin/systemd-run",
            "--user",
            "--quiet",
            f"--unit={unit}",
            f"--slice={parent_slice}",
            f"--setenv=KACHE_SOCKET_PATH={socket}",
            "--setenv=PYTHONPATH=" + os.environ.get("PYTHONPATH", ""),
            sys.executable,
            str(FIXTURES / "fake_kache.py"),
            "daemon",
            "run",
        ],
        check=True,
        env=manager_env(),
        timeout=30,
    )
    if not wait_for(lambda: socket.exists() and _is_active(unit)):
        msg = f"{unit} did not start the fake kache daemon"
        raise RuntimeError(msg)


def _is_active(unit: str) -> bool:
    """Report whether systemd still considers a unit running.

    Parameters
    ----------
    unit : str
        The unit to query.

    Returns
    -------
    bool
        Whether the unit is active.
    """
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        ["/usr/bin/systemctl", "--user", "is-active", "--quiet", unit],
        env=manager_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return result.returncode == 0
