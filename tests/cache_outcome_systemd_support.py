"""Owned manager fixture and world for cache-outcome acceptance scenarios."""

import contextlib
import json

# ruff: ignore[suspicious-subprocess-import] - fixture-owned argv vectors never request a shell.
import subprocess
import sys
import time
import typing as typ

import pytest
from harness_units import manager_env
from systemd_harness import Harness, HarnessOptions, kill_group, wait_for

from buildlimits.outcome_record_format import OutcomeRecord, StoreRecord, WindowSummary

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


def replay_report(
    hypothesis: typ.Literal["h2", "h3"], store_path: Path
) -> dict[str, object]:
    """Run one installed read-only replay against an isolated store.

    The real-manager scenarios use this boundary so they exercise the same
    command-line JSON interface as an operator, while each step asserts its
    own hypothesis-specific contract.

    Example
    -------
    A scenario reads its H2 result with
    ``replay_report("h2", store_path)["verdict"]``.

    Returns
    -------
    dict[str, object]
        The replay command's decoded JSON object.

    Raises
    ------
    TypeError
        If the command does not return a JSON object.
    """
    # ruff: ignore[subprocess-without-shell-equals-true] - fixed Python module argv.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "buildlimits.outcome_replay_cli",
            hypothesis,
            "--store",
            str(store_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    report: object = json.loads(result.stdout)
    if not isinstance(report, dict):
        msg = "the replay command did not return a JSON object"
        raise TypeError(msg)
    return {str(key): value for key, value in report.items()}


class OutcomeWorld:
    """One isolated record-mode supervisor and its completed fixture requests."""

    def __init__(self) -> None:
        """Create only owned harness state; the background starts its units."""
        self.harness = Harness(
            2,
            cached=True,
            options=HarnessOptions(cache_outcomes="record"),
        )
        self.processes: list[subprocess.Popen[bytes]] = []
        self.old_compile_requests = 0
        self.old_reading_events = 0
        self.second_elapsed: float | None = None
        self.barrier_queries_before = 0

    def start(self) -> None:
        """Start the real user units and wait for the initial statistics baseline."""
        self.harness.configure_fake_cache()
        self.harness.start()
        if not wait_for(self._has_baseline, timeout=20):
            pytest.fail(self.harness.journal())

    def _has_baseline(self) -> bool:
        """Report whether the first statistics reading has completed.

        Returns
        -------
        bool
            Whether the baseline reading has completed.
        """
        return self.harness.status().get("cache_outcomes", {}).get("readings", 0) >= 1

    def compile(
        self,
        *,
        metadata: str = "fixture",
        env: dict[str, str] | None = None,
    ) -> float:
        """Run one cacheable request and return its elapsed admission time.

        Returns
        -------
        float
            Elapsed monotonic time for the completed request.
        """
        started = time.monotonic()
        process = self.harness.compile_rustc(metadata=metadata, env=env)
        self.processes.append(process)
        _stdout, stderr = process.communicate(timeout=30)
        elapsed = time.monotonic() - started
        if process.returncode != 0:
            pytest.fail(stderr.decode(errors="replace"))
        return elapsed

    def wait_for_outcomes(self, count: int) -> list[dict[str, str]]:
        """Wait for both journal events and durable records for a count.

        Returns
        -------
        list[dict[str, str]]
            The cache-outcome events collected by the real supervisor.
        """
        has_events = wait_for(
            lambda: len(self.harness.read_metric_events("cache_outcome")) >= count,
            timeout=15,
        )
        has_records = wait_for(
            lambda: len(self._records()) >= count,
            timeout=15,
        )
        if not has_events or not has_records:
            pytest.fail(self.harness.journal())
        return self.harness.read_metric_events("cache_outcome")

    def _records(self) -> list[StoreRecord]:
        """Return store rows after asynchronous decoding succeeds.

        Returns
        -------
        list[StoreRecord]
            The currently decoded outcome and window rows.
        """
        store = self.harness.outcome_store()
        if store is None or "failure" in store:
            return []
        records = store.get("records")
        if not isinstance(records, (list, tuple)):
            return []
        return [
            record
            for record in records
            if isinstance(record, (OutcomeRecord, WindowSummary))
        ]

    def restart_cache(self) -> None:
        """Restart only this world's fake server and save reading position."""
        state = self.harness.fake_cache_state()
        self.old_compile_requests = state["counters"]["compile_requests"]
        old_events = self.harness.read_metric_events("outcome_reading")
        self.old_reading_events = len(old_events)
        # ruff: ignore[subprocess-without-shell-equals-true] - fixed systemctl argv.
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "restart", self.harness.sccache_unit],
            env=manager_env(),
            check=True,
            timeout=45,
        )

    def wait_for_generation_change(self) -> None:
        """Wait for a post-restart compile to observe the new identity."""
        has_new_generation = wait_for(
            lambda: any(
                row.get("BUILD_LIMITS_OUTCOME_REASON") == "generation_changed"
                for row in self.harness.read_metric_events("outcome_reading")[
                    self.old_reading_events :
                ]
            ),
            timeout=15,
        )
        if not has_new_generation:
            pytest.fail(self.harness.journal())

    def close(self) -> None:
        """Kill unfinished fixture requests before stopping the owned units."""
        for process in self.processes:
            if process.poll() is None:
                kill_group(process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        self.harness.close()


@pytest.fixture
def outcome_world() -> cabc.Iterator[OutcomeWorld]:
    """Give each BDD scenario a fresh supervisor and fake cache.

    Yields
    ------
    OutcomeWorld
        An isolated supervisor and its fake cache state.
    """
    world = OutcomeWorld()
    try:
        yield world
    finally:
        world.close()
