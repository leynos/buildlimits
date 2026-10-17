"""Harness-owned runtime units exercise the production cache readiness order.

Static UUID-named units let startup inspect an inactive, loaded cache service
before Type=notify releases its start job. This fixture manages only its own
units and files; compiler token and request ownership stay with the harness.
"""

import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed fixture argv, never a shell.
import sys
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    from systemd_harness import Harness


def _exec_path(path: Path) -> str:
    """Quote a known executable path in systemd's unit syntax.

    Returns
    -------
    str
        Quoted path with literal percent, slash and quote escapes.
    """
    escaped = str(path).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _wrapper(path: Path, argv: list[str], environment: dict[str, str]) -> None:
    """Write a fixture launcher using Python argv, without a shell expansion."""
    path.write_text(
        f"#!{sys.executable}\nimport os\n"
        f"os.environ.update({environment!r})\n"
        f"os.execv({sys.executable!r}, {argv!r})\n"
    )
    path.chmod(0o700)


class CachedUnits:
    """Install isolated supervisor/cache units with a readiness dependency.

    Parameters
    ----------
    harness : Harness
        The owner of the unique unit names, runtime and compiler pool.
    environment : dict[str, str]
        Command environment selecting the harness's reachable user manager.
    """

    def __init__(self, harness: Harness, environment: dict[str, str]) -> None:
        """Remember the owner without installing or starting any unit."""
        self.harness = harness
        self.environment = environment
        self.paths: list[Path] = []

    def _run(self, *arguments: str) -> None:
        """Run one bounded manager command for this fixture."""
        subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - owned fixture units and fixed argv.
            ["/usr/bin/systemctl", "--user", *arguments],
            env=self.environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

    def _install_file(self, name: str, text: str) -> None:
        """Create a unique runtime unit without replacing an existing file."""
        directory = Path(f"/run/user/{os.getuid()}/systemd/user")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        with path.open("x", encoding="utf-8") as output:
            self.paths.append(path)
            output.write(text)

    def install(self, supervisor: list[str], server: list[str]) -> None:
        """Install loaded, inactive units before admission checks old work.

        Parameters
        ----------
        supervisor : list[str]
            The isolated supervisor's Python executable and arguments.
        server : list[str]
            The isolated fake cache server's Python executable and arguments.
        """
        harness = self.harness
        python_path = {"PYTHONPATH": os.environ.get("PYTHONPATH", "")}
        socket_path = _exec_path(harness.runtime / "sccache.sock")
        ready = harness.runtime / "supervisor-launch.py"
        cache = harness.runtime / "cache-launch.py"
        _wrapper(ready, supervisor, python_path)
        _wrapper(
            cache,
            server,
            {
                **python_path,
                "SCCACHE_SERVER_UDS": str(harness.runtime / "sccache.sock"),
            },
        )
        limits = (
            ""
            if harness.nofile is None
            else f"LimitNOFILE={harness.nofile[0]}:{harness.nofile[1]}\n"
        )
        self._install_file(
            harness.parent_slice,
            "[Unit]\nDescription=Isolated admission fixture\n[Slice]\n",
        )
        self._install_file(
            harness.unit,
            f"[Unit]\nDescription=Isolated admission supervisor\n"
            f"Wants={harness.sccache_unit}\nBefore={harness.sccache_unit}\n"
            f"[Service]\nType=notify\nNotifyAccess=main\nSlice={harness.parent_slice}\n"
            f"Restart=always\nRestartPreventExitStatus=2\nRestartSec=1\n"
            f"TimeoutStartSec=30\nTimeoutStopSec=infinity\n"
            f"ExecStart={_exec_path(ready)}\n{limits}",
        )
        self._install_file(
            harness.sccache_unit,
            f"[Unit]\nDescription=Isolated managed cache\nAfter={harness.unit}\n"
            # Mirrors the role template's restart barrier, so the fixture unit
            # refuses to start while a recovery transaction is in flight.
            f"ConditionPathExists=!{harness.runtime / 'cache-recovery'}\n"
            f"[Service]\nSlice={harness.parent_slice}\nRestart=no\n"
            # Mirrors the role template's own pre-start, so a restart under a
            # recovery transaction can rebind rather than fail on a stale socket.
            f"ExecStartPre=/usr/bin/rm -f {socket_path}\n"
            f"ExecStart={_exec_path(cache)}\n",
        )
        self._run("daemon-reload")

    def start(self) -> None:
        """Start admission; systemd orders the cache after its READY message."""
        self._run("start", self.harness.unit)

    def close(self) -> None:
        """Remove only owned unit files after the harness has stopped its units."""
        for path in tuple(self.paths):
            path.unlink(missing_ok=True)
            self.paths.remove(path)
        self._run("daemon-reload")
