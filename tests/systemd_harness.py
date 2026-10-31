"""A real supervisor on the running systemd user manager, for acceptance tests.

The harness runs the supervisor as a transient user service with a FIFO
standing in for ``/dev/guild``: a FIFO primed with ``N`` bytes behaves like a
guildmaster pool for a single reader that returns what it takes. Every
harness uses its own runtime directory, slice and unit names, so concurrent
runs and leftovers from failed runs never collide, and it removes only what
it created.
"""

import contextlib
import dataclasses as dc
import os
import shutil
import subprocess
import sys
import tempfile
import typing as typ
import uuid
from pathlib import Path

from cache_outcome_harness import CacheOutcomeHarnessMixin
from cached_unit_files import CachedUnits
from harness_config import write_config
from harness_processes import AdapterProcessOptions, start_adapter, wait_for
from harness_processes import journal as journal
from harness_processes import kill_group as kill_group
from harness_processes import pid_alive as pid_alive
from harness_status import HarnessStatus
from harness_units import (
    Placement,
    listening,
    manager_env,
    run_unit,
)
from harness_units import (
    systemd_user_marks as systemd_user_marks,
)
from harness_units import unit_active as _unit_active
from harness_units import (
    user_manager_available as user_manager_available,
)
from systemd_harness_tokens import TokenObservationMixin

if typ.TYPE_CHECKING:
    import collections.abc as cabc

FIXTURES = Path(__file__).with_name("fixtures")

__all__ = [
    "Harness",
    "HarnessOptions",
    "Placement",
    "journal",
    "kill_group",
    "listening",
    "manager_env",
    "pid_alive",
    "run_unit",
    "systemd_user_marks",
    "user_manager_available",
    "wait_for",
]


@dc.dataclass(frozen=True, slots=True)
class HarnessOptions:
    """Feature settings shared by the isolated systemd scenarios."""

    cache_recovery: str = "starved"
    cache_outcomes: str = "off"


_DEFAULT_HARNESS_OPTIONS = HarnessOptions()


class Harness(CacheOutcomeHarnessMixin, TokenObservationMixin, HarnessStatus):
    """One supervisor, its pool and optionally a fake sccache server.

    Parameters
    ----------
    slots : int
        The pool's capacity.
    cached : bool
        Whether to compose compilers with the fake sccache server.
    nofile : tuple[int, int] | None
        Optional supervisor soft/hard descriptor limit for pressure tests.
    options : HarnessOptions
        Optional cache-recovery and cache-outcome feature settings.
    """

    def __init__(
        self,
        slots: int,
        *,
        cached: bool = False,
        nofile: tuple[int, int] | None = None,
        options: HarnessOptions = _DEFAULT_HARNESS_OPTIONS,
    ) -> None:
        """Create the harness's runtime, work directory, pool and config."""
        tag = uuid.uuid4().hex[:8]
        self.tag = tag
        runtime_base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        self.runtime = Path(tempfile.mkdtemp(prefix="blt-", dir=runtime_base))
        self.work = Path(tempfile.mkdtemp(prefix="blt-work-"))
        self.parent_slice = f"blt{tag}.slice"
        self.compilers_slice = f"blt{tag}-compilers.slice"
        self.unit = f"blt{tag}-supervisor.service"
        self.sccache_unit = f"blt{tag}-sccache.service"
        self.slots = slots
        self.cached = cached
        self.nofile = nofile
        self.cache_recovery = options.cache_recovery
        self.cache_outcomes = options.cache_outcomes
        self.placement = Placement(self.parent_slice, nofile)
        self.cached_units: CachedUnits | None = None
        # Test seams for the cached supervisor unit: a directory of command
        # shims ahead of its PATH, and a KillMode override (None keeps the
        # fixture unit's own).
        self.path_prefix: Path | None = None
        self.kill_mode: str | None = None
        self.pool = self.runtime / "pool"
        os.mkfifo(self.pool)
        self._holder: int | None = os.open(self.pool, os.O_RDWR | os.O_CLOEXEC)
        os.write(self._holder, b"+" * slots)
        self.adapter = Path(sys.executable).with_name("build-limits-rustc")
        self.status_command = Path(sys.executable).with_name("build-limits-status")
        self.env: dict[str, str] = {
            **os.environ,
            "BUILD_LIMITS_SOCKET": str(self.runtime / "supervisor.sock"),
            "BUILD_LIMITS_COMPILERS_SLICE": self.compilers_slice,
            "BUILD_LIMITS_HEARTBEAT_SECS": "600",
            "BUILD_LIMITS_UNAVAILABLE_SECS": "30",
            "XDG_STATE_HOME": str(self.state_home),
        }
        for inherited in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"):
            self.env.pop(inherited, None)
        write_config(self)

    def start(self) -> None:
        """Start the supervisor (and fake server) and wait until it listens.

        Raises
        ------
        RuntimeError
            If either service does not come up; the message carries the
            supervisor's journal when it is the one that failed.
        """
        script = (
            "import sys; from buildlimits.service import main; "
            f"sys.exit(main(['--config', '{self.runtime / 'supervisor.toml'}']))"
        )
        argv = [sys.executable, "-c", script]
        if self.cached:
            self._start_cached(argv)
        else:
            run_unit(self.unit, argv, {}, self.placement)
        if not wait_for(self._listening):
            raise RuntimeError(self.journal())
        if self.cached and not wait_for(lambda: self.status()["caching"], timeout=20):
            msg = "the isolated supervisor did not observe its ready managed cache"
            raise RuntimeError(msg)

    def _start_cached(self, argv: list[str]) -> None:
        """Install owned cache composition once and wait for its ordered startup.

        Raises
        ------
        RuntimeError
            If the fake cache does not start after admission becomes ready.
        """
        if self.cached_units is None:
            self.cached_units = CachedUnits(self, manager_env())
            self.cached_units.install(
                argv, [sys.executable, str(FIXTURES / "fake_sccache.py"), "server"]
            )
        self.cached_units.start()
        if not wait_for((self.runtime / "sccache.sock").exists):
            msg = "the fake sccache server did not start after admission READY"
            raise RuntimeError(msg)

    def _listening(self) -> bool:
        """Report whether the supervisor accepts connections.

        Returns
        -------
        bool
            Whether a connection succeeded.
        """
        return listening(self.runtime)

    def compile(
        self,
        script: str,
        *,
        cwd: Path | None = None,
        piped: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start an adapter whose "compiler" is a shell script.

        Parameters
        ----------
        script : str
            The shell script to run as the compiler.
        cwd : Path | None
            The working directory; the harness's work directory by default.
        piped : bool
            Whether to connect all three standard streams to pipes.
        env : dict[str, str] | None
            Overrides for this caller's environment, so a build can set the
            fixture cache's detached-work variables for its own request only.

        Returns
        -------
        subprocess.Popen[bytes]
            The running adapter.
        """
        return self.invoke(("/bin/sh", "-c", script), cwd=cwd, piped=piped, env=env)

    def invoke(
        self,
        argv: cabc.Sequence[str],
        *,
        cwd: Path | None = None,
        piped: bool = False,
        env: cabc.Mapping[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start the adapter with arbitrary argv and an optional env overlay.

        Parameters
        ----------
        argv : Sequence[str]
            The argument vector passed to the adapter unchanged.
        cwd : Path | None
            The caller's working directory; the harness's work directory by default.
        piped : bool
            Whether to connect all three standard streams to pipes.
        env : Mapping[str, str] | None
            Overrides for this caller, merged over the harness environment.

        Returns
        -------
        subprocess.Popen[bytes]
            The running adapter in its own session.

        Examples
        --------
        Run a version query with captured output::

            process = harness.invoke(("rustc", "-vV"), piped=True)
            stdout, stderr = process.communicate(timeout=2)
        """
        return start_adapter(
            self.adapter,
            argv,
            AdapterProcessOptions(
                cwd=cwd or self.work,
                base_env=self.env,
                piped=piped,
                env=env,
            ),
        )

    def kill_supervisor(self) -> None:
        """Kill the supervisor outright, as a crash would."""
        subprocess.run(
            ["systemctl", "--user", "kill", "--signal=SIGKILL", self.unit],
            check=False,
            env=manager_env(),
            timeout=30,
        )
        wait_for(lambda: not _unit_active(self.unit))
        subprocess.run(
            ["systemctl", "--user", "reset-failed", self.unit],
            check=False,
            env=manager_env(),
            timeout=30,
        )
        self._reclaim_tokens()

    def _reclaim_tokens(self) -> None:
        """Refill the pool, as guildmaster does when a client's handle closes.

        A FIFO cannot know that its reader died holding tokens. Guildmaster
        returns a closed client's balance, so the harness restores the full
        capacity after killing the supervisor.
        """
        if self._holder is None:
            return
        os.set_blocking(self._holder, False)
        with contextlib.suppress(BlockingIOError):
            while os.read(self._holder, 64):
                pass
        os.set_blocking(self._holder, True)
        os.write(self._holder, b"+" * self.slots)

    def stop_sccache(self) -> None:
        """Stop the fake sccache server's unit, as an outage would."""
        subprocess.run(
            ["systemctl", "--user", "stop", self.sccache_unit],
            env=manager_env(),
            timeout=30,
            capture_output=True,
            check=False,
        )

    def journal(self) -> str:
        """Return the supervisor's journal, for failure messages.

        Returns
        -------
        str
            Recent journal lines.
        """
        return journal(self.unit)

    def close(self) -> None:
        """Stop only owned units and release the pool handle exactly once."""
        try:
            # A stopped cache is the proof needed by retained server-drain tokens.
            for unit in (self.sccache_unit, self.unit, self.parent_slice):
                subprocess.run(
                    ["systemctl", "--user", "stop", unit],
                    capture_output=True,
                    check=False,
                    env=manager_env(),
                    timeout=90,
                )
                subprocess.run(
                    ["systemctl", "--user", "reset-failed", unit],
                    capture_output=True,
                    check=False,
                    env=manager_env(),
                    timeout=30,
                )
            if self.cached_units is not None:
                units, self.cached_units = self.cached_units, None
                units.close()
        finally:
            holder, self._holder = self._holder, None
            if holder is not None:
                with contextlib.suppress(OSError):
                    os.close(holder)
            shutil.rmtree(self.runtime, ignore_errors=True)
            shutil.rmtree(self.work, ignore_errors=True)
