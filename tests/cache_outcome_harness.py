"""Outcome-specific controls and observations for the systemd harness."""

import dataclasses as dc
import json
import shlex
import shutil

# ruff: ignore[suspicious-subprocess-import] - fixture-owned argv vectors never request a shell.
import subprocess
import sys
import typing as typ
from pathlib import Path

from harness_units import manager_env


class FakeCacheState(typ.TypedDict, total=False):
    """Identifier-free counters persisted by the fake sccache server."""

    counters: dict[str, int]
    script_index: int
    statistics_queries: int


class OutcomeHarness(typ.Protocol):
    """Systemd harness attributes used by the outcome helper methods."""

    work: Path
    runtime: Path
    env: dict[str, str]
    adapter: Path
    unit: str
    sccache_unit: str

    @property
    def fake_config(self) -> Path:
        """The fake server's optional scripting file."""

    @property
    def fake_rustc(self) -> Path:
        """The executable fake compiler used by cacheable requests."""

    @property
    def state_home(self) -> Path:
        """The isolated state root used by the supervisor."""

    def run_adapter(
        self,
        compiler: Path,
        arguments: list[str],
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start the adapter with one compiler and its fixed argument vector.

        Returns
        -------
        subprocess.Popen[bytes]
            The running process with captured output streams.
        """

    def read_metric_events(self, event: str) -> list[dict[str, str]]:
        """Read structured journal records for one fixed event name.

        Returns
        -------
        list[dict[str, str]]
            Matching structured records.
        """

    def fake_cache_state(self) -> FakeCacheState:
        """Read the fake server's persistent counter snapshot.

        Returns
        -------
        FakeCacheState
            Identifier-free fake server counters and script position.
        """


@dc.dataclass(frozen=True, slots=True)
class FakeCacheConfiguration:
    """Scripted fake-cache outcomes and bounded response delays."""

    script: list[str] | None = None
    statistics_delay_seconds: float = 0.0
    statistics_delay_count: int = 0
    hit_delay_seconds: float = 0.0
    miss_delay_seconds: float = 0.0
    rustc_sleep_emit_metadata: str | None = None
    rustc_emit_seconds: float = 0.0


_DEFAULT_FAKE_CACHE_CONFIGURATION = FakeCacheConfiguration()


class CacheOutcomeHarnessMixin:
    """Add fake-cache recording controls without enlarging the core harness."""

    def run_adapter(
        self: OutcomeHarness,
        compiler: Path,
        arguments: list[str],
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start one compiler argument vector through the adapter.

        Parameters
        ----------
        compiler : Path
            The compiler executable passed to the adapter.
        arguments : list[str]
            The fixed compiler arguments.
        env : dict[str, str] | None
            Optional caller environment overrides.

        Returns
        -------
        subprocess.Popen[bytes]
            The running adapter with captured output streams.
        """
        caller = self.env if env is None else {**self.env, **env}
        # ruff: ignore[subprocess-without-shell-equals-true] - fixed adapter argv, no shell.
        return subprocess.Popen(
            [str(self.adapter), str(compiler), *arguments],
            cwd=self.work,
            env=caller,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def read_metric_events(self: OutcomeHarness, event: str) -> list[dict[str, str]]:
        """Read structured journal records for one fixed event name.

        Parameters
        ----------
        event : str
            The fixed ``BUILD_LIMITS_EVENT`` value to select.

        Returns
        -------
        list[dict[str, str]]
            Matching records emitted by this owned user unit.
        """
        # ruff: ignore[subprocess-without-shell-equals-true] - fixed journalctl argv.
        result = subprocess.run(
            [
                "/usr/bin/journalctl",
                "--user",
                "--unit",
                self.unit,
                "--output=json",
                "--no-pager",
            ],
            env=manager_env(),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return [
            record
            for line in result.stdout.splitlines()
            if (record := json.loads(line)).get("BUILD_LIMITS_EVENT") == event
        ]

    @property
    def state_home(self: OutcomeHarness) -> Path:
        """The isolated state root used by the supervisor."""
        return self.work / "state"

    @property
    def fake_config(self: OutcomeHarness) -> Path:
        """The fake server's optional scripting file."""
        return self.runtime / "fake-sccache-config.json"

    @property
    def fake_rustc(self: OutcomeHarness) -> Path:
        """The executable fake compiler used by cacheable requests."""
        return self.work / "rustc"

    def compile_rustc(
        self: OutcomeHarness,
        *,
        metadata: str = "fixture",
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start one valid cacheable Rust request through the adapter.

        Returns
        -------
        _RunningAdapter
            The running request process with captured output streams.
        """
        if not self.fake_rustc.exists():
            fixture = Path(__file__).with_name("fake_rustc.py")
            implementation = self.work / "fake_rustc_impl.py"
            shutil.copy2(fixture, implementation)
            implementation.chmod(0o700)
            self.fake_rustc.write_text(
                f"#!/usr/bin/bash\n"
                f"exec -a rustc {shlex.quote(sys.executable)} "
                f'{shlex.quote(str(implementation))} "$@"\n',
                encoding="utf-8",
            )
            self.fake_rustc.chmod(0o700)
        source = self.work / "fixture.rs"
        source.write_text("pub fn fixture() {}\n", encoding="utf-8")
        arguments = [
            "--crate-name=cache_outcome_fixture",
            "--crate-type=rlib",
            "--emit=link",
            "--out-dir",
            str(self.work / "out"),
            "-C",
            f"metadata={metadata}",
            str(source),
        ]
        return self.run_adapter(self.fake_rustc, arguments, env)

    def configure_fake_cache(
        self: OutcomeHarness,
        configuration: FakeCacheConfiguration = _DEFAULT_FAKE_CACHE_CONFIGURATION,
    ) -> None:
        """Set scripted fake results and a bounded number of delayed readings."""
        self.fake_config.write_text(
            json.dumps({
                "script": [] if configuration.script is None else configuration.script,
                "statistics_delay_seconds": configuration.statistics_delay_seconds,
                "statistics_delay_count": configuration.statistics_delay_count,
                "hit_delay_seconds": configuration.hit_delay_seconds,
                "miss_delay_seconds": configuration.miss_delay_seconds,
                "rustc_sleep_emit_metadata": configuration.rustc_sleep_emit_metadata,
                "rustc_emit_seconds": configuration.rustc_emit_seconds,
            }),
            encoding="utf-8",
        )

    def fake_cache_state(self: OutcomeHarness) -> FakeCacheState:
        """Read the fake server's persistent, identifier-free counter snapshot.

        Returns
        -------
        FakeCacheState
            The current fake server state, or an empty mapping before startup.
        """
        state_path = self.runtime / "fake-sccache-state.json"
        if not state_path.exists():
            return {}
        return typ.cast(
            "FakeCacheState",
            json.loads(state_path.read_text(encoding="utf-8")),
        )

    def outcome_store(self: OutcomeHarness) -> dict[str, object] | None:
        """Read the per-harness store only after it has been initialized.

        Returns
        -------
        dict[str, object] | None
            Decoded store rows, a bounded failure reason, or None before setup.
        """
        from buildlimits import outcome_records, outcome_store_io

        path = self.state_home / "buildlimits" / "cache-outcomes.jsonl"
        try:
            decoded = outcome_store_io.read_store(path)
        except FileNotFoundError:
            return None
        if isinstance(decoded, outcome_records.RecordFailure):
            return {"failure": decoded.reason}
        return {"records": decoded.records}

    def server_children(self: OutcomeHarness) -> int:
        """Count compilations the fake cache server is running.

        Returns
        -------
        int
            The current number of fake server compiler children.
        """
        from buildlimits.sccache import ManagedSccache

        return ManagedSccache(
            self.sccache_unit, self.runtime / "sccache.sock"
        ).active_compiles()
