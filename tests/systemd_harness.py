"""A real supervisor on the running systemd user manager, for acceptance tests.

The harness runs the supervisor as a transient user service with a FIFO
standing in for ``/dev/guild``: a FIFO primed with ``N`` bytes behaves like a
guildmaster pool for a single reader that returns what it takes. Every
harness uses its own runtime directory, slice and unit names, so concurrent
runs and leftovers from failed runs never collide, and it removes only what
it created.
"""

import array
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import termios
import typing as typ
import uuid
from pathlib import Path

from cached_unit_files import CachedUnits
from harness_config import KacheSettings as KacheSettings
from harness_config import write_config
from harness_processes import journal as journal
from harness_processes import kill_group as kill_group
from harness_processes import pid_alive as pid_alive
from harness_processes import server_children as server_children
from harness_units import Placement, listening, manager_env, run_unit, stop_units
from harness_units import systemd_user_marks as systemd_user_marks
from harness_units import unit_active as _unit_active
from harness_units import user_manager_available as user_manager_available
from harness_units import wait_for as wait_for
from kache_harness_steps import KacheHarnessSteps

FIXTURES = Path(__file__).with_name("fixtures")


class Harness(KacheHarnessSteps):
    """One supervisor, its pool and optionally a fake cache backend.

    Parameters
    ----------
    slots : int
        The pool's capacity.
    cached : bool
        Whether to compose compilers with the fake sccache server.
    kache : KacheSettings | None
        Whether to compose compilers with kache instead, pointing them at a
        fake daemon this harness starts as its own unit. A setting that names
        its own client and does not expect it to serve configures kache
        without starting a daemon, so the scenario can observe the fallback.
    nofile : tuple[int, int] | None
        Optional supervisor soft/hard descriptor limit for pressure tests.
    cache_recovery : str
        The supervisor's recovery policy; ``"manual"`` leaves every
        transaction to the operator's own command.
    """

    # The constructor is the one keyword switchboard every scenario configures,
    # so it names each backend and seam separately rather than hiding them in a
    # settings object a reader would have to look up. `pyproject.toml` records
    # the same choice for Ruff's copy of this check.
    def __init__(  # pylint: disable=too-many-arguments
        self,
        slots: int,
        *,
        cached: bool = False,
        kache: KacheSettings | None = None,
        nofile: tuple[int, int] | None = None,
        cache_recovery: str = "starved",
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
        self.kache_unit = f"blt{tag}-kache.service"
        self.slots = slots
        self.cached = cached
        self.kache = kache or KacheSettings()
        # A kache group is only composed when a daemon already answers on this
        # socket, so it is exported to the supervisor, to status and to each
        # group rather than left to kache's own default resolution.
        self.kache_socket = self.runtime / "kache.sock"
        self.kache_store = self.runtime / "kache-store"
        self.nofile = nofile
        self.cache_recovery = cache_recovery
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
        }
        for inherited in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"):
            self.env.pop(inherited, None)
        write_config(self)

    def start(self) -> None:
        """Start the daemon, the supervisor and the fake server, then wait.

        The kache daemon is started *before* the supervisor and is owned by
        this harness alone, never by the supervisor: an admitted group must
        only ever be pointed at a daemon that is already answering. See ADR
        005.

        Raises
        ------
        RuntimeError
            If a service does not come up; the message carries the
            supervisor's journal when it is the one that failed.
        """
        if self.kache.enabled and self.kache.serving:
            self._start_kache_daemon()
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
        wants_cache = self.cached or (self.kache.enabled and self.kache.serving)
        if wants_cache and not wait_for(lambda: self.status()["caching"], timeout=20):
            msg = "the isolated supervisor did not observe its ready cache"
            raise RuntimeError(msg)
        if self.kache.enabled and not self.kache.serving:
            self._await_kache_fallback()

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
        stream = subprocess.PIPE if piped else None
        caller = self.env if env is None else {**self.env, **env}
        return subprocess.Popen(
            [str(self.adapter), "/bin/sh", "-c", script],
            cwd=cwd or self.work,
            env=caller,
            start_new_session=True,
            stdin=stream,
            stdout=stream,
            stderr=stream,
            text=False,
        )

    def status(self) -> dict[str, typ.Any]:
        """Read the supervisor's status document.

        Returns
        -------
        dict[str, Any]
            The status.
        """
        result = subprocess.run(
            [str(self.status_command), "--json"],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return json.loads(result.stdout)

    def readiness_status(self) -> tuple[int, dict[str, typ.Any]]:
        """Read status with the admission-readiness exit contract.

        Returns
        -------
        tuple[int, dict[str, Any]]
            The ``--ready`` exit code and the decoded status document.
        """
        result = subprocess.run(
            [str(self.status_command), "--ready", "--json"],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return result.returncode, json.loads(result.stdout)

    def drain_fault(self) -> str:
        """Read the current retained-drain fault, if one is reported.

        Returns
        -------
        str
            The reported fault, or an empty string when no fault exists.
        """
        fault = self.status()["drain_fault"]
        return fault if isinstance(fault, str) else ""

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
        stop_units((self.sccache_unit,))

    def server_children(self) -> int:
        """Count compilations the fake sccache server is running.

        Returns
        -------
        int
            Direct children of the server process.
        """
        return server_children(self.sccache_unit, self.runtime / "sccache.sock")

    def journal(self) -> str:
        """Return the supervisor's journal, for failure messages.

        Returns
        -------
        str
            Recent journal lines.
        """
        return journal(self.unit)

    def free_tokens(self) -> int:
        """Read the pool without taking a token, as an observer would.

        Returns
        -------
        int
            Bytes waiting in the FIFO, which are the free tokens. A token held
            by admission is missing from this count, so a recovery can be
            checked for having released exactly what it accounted for without
            disturbing the pool.
        """
        if self._holder is None:
            return 0
        pending = array.array("i", [0])
        # FIONREAD's fourth argument is the kernel's own all-or-nothing flag,
        # and ioctl accepts no keywords, so the flag cannot be named.
        fcntl.ioctl(
            self._holder,
            termios.FIONREAD,
            pending,
            True,  # ruff: ignore[boolean-positional-value-in-call] - kernel flag, never a caller's.
        )
        return int(pending[0])

    def close(self) -> None:
        """Stop only owned units and release the pool handle exactly once."""
        try:
            # A stopped cache is the proof needed by retained server-drain
            # tokens. The parent slice goes last, so every unit inside it stops
            # first; a kache daemon stops with everything else owned here.
            stop_units((
                self.sccache_unit,
                self.kache_unit,
                self.unit,
                self.parent_slice,
            ))
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
