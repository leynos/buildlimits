"""Scenario world for the opt-in systemd cache-recovery acceptance tests."""

import contextlib
import os
import subprocess
from pathlib import Path

from harness_units import manager_env
from systemd_harness import Harness, HarnessOptions, kill_group, wait_for


class RecoveryWorld:
    """One isolated cached supervisor and the requesters it admits.

    Every build gets its own jobserver FIFO, held open read-write by this
    world. The fixture cache blocks acquiring that FIFO before it spawns its
    compiler, so an empty FIFO *is* the pre-spawn wait and the fixture's own
    ``.waiting`` marker proves the wait is armed. Most scenarios use the
    ``manual`` recovery policy; one selects ``starved`` before start to check
    automatic progress once demand arrives.
    """

    def __init__(self) -> None:
        """Create only fixture ownership; units start in the scenario."""
        self.harness = Harness(
            2,
            cached=True,
            options=HarnessOptions(cache_recovery="manual"),
        )
        self.fifos: list[Path] = []
        self.holders: list[int] = []
        self.processes: list[subprocess.Popen[bytes]] = []
        self.cancelled: list[subprocess.Popen[bytes]] = []
        self.queued: subprocess.Popen[bytes] | None = None
        self.recovered: subprocess.CompletedProcess[str] | None = None

    def start_waiting_build(self) -> subprocess.Popen[bytes]:
        """Start a cached build that stops in the pre-spawn jobserver wait.

        The FIFO is created and held read-write here, so a blocking acquire
        can only be the intended wait. The fixture server writes the
        ``.waiting`` marker after it accepts the request and immediately
        before that blocking read, so a written marker means this build has
        been admitted and no compiler has spawned for it.

        Returns
        -------
        subprocess.Popen[bytes]
            The admitted adapter, still waiting on the cache's jobserver.
        """
        jobserver = self.harness.work / f"jobserver-{len(self.fifos)}"
        os.mkfifo(jobserver)
        self.holders.append(os.open(jobserver, os.O_RDWR))
        self.fifos.append(jobserver)
        process = self.harness.compile(
            "true", env={"FAKE_SCCACHE_JOBSERVER": str(jobserver)}
        )
        self.processes.append(process)
        marker = Path(f"{jobserver}.waiting")
        assert wait_for(marker.exists), self.harness.journal()
        return process

    def cache_active(self) -> bool:
        """Report whether the fixture cache's unit is active.

        Returns
        -------
        bool
            Whether the manager currently reports the isolated cache active.
        """
        return (
            subprocess.run(
                [
                    "/usr/bin/systemctl",
                    "--user",
                    "is-active",
                    "--quiet",
                    self.harness.sccache_unit,
                ],
                check=False,
                env=manager_env(),
                timeout=30,
            ).returncode
            == 0
        )

    def stop_cache(self) -> None:
        """Stop the fixture cache's unit, as the transaction's own stop does."""
        self.harness.stop_sccache()
        assert wait_for(lambda: not self.cache_active(), timeout=30), (
            self.harness.journal()
        )

    def start_cache(self) -> None:
        """Ask the manager to start the fixture cache's unit."""
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "start", self.harness.sccache_unit],
            check=False,
            env=manager_env(),
            timeout=30,
        )

    def close(self) -> None:
        """Stop owned units, then reap processes and release held FIFOs."""
        self.harness.close()
        for process in self.processes:
            if process.poll() is None:
                kill_group(process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=30)
        for descriptor in self.holders:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        self.holders = []
