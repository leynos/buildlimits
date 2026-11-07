"""Opt-in acceptance checks for a real kache client and daemon.

The unit suites script kache's wire documents, which proves the adapter reads
what kache documents but not that a real release writes it. This module closes
that gap end to end: a real daemon, a real client composed by the supervisor,
and a real compilation of a Cargo-shaped library request. Three things must be
true of that path, and each is checked where kache itself publishes it:

- health: :meth:`buildlimits.kache.Kache.is_reachable` is ``True`` only while
  the daemon answers, and the supervisor's own cache reading follows it;
- a cacheable compilation: the composed group compiles through kache and
  writes the artefacts the compiler produced;
- an observable cache result: the store's own event log records the first
  request as a ``miss`` that ran the compiler and the replay as a
  ``local_hit`` that did not, and ``kache stats --full`` agrees.

See ``docs/adr-006-kache-cache-backend.md``.
"""

import shutil
import subprocess
import sys
import typing as typ
from pathlib import Path

from kache_host import (
    _base_environment,
    _private_daemon,
    _read_events,
    _read_summary,
    kache_host_marks,
)
from request_helpers import launch_pending
from supervisor_helpers import _empty_runner

from buildlimits.config import SupervisorConfig
from buildlimits.kache import Kache
from buildlimits.supervisor import Supervisor
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    from buildlimits.launch import LaunchSpec

pytestmark = kache_host_marks()


def _toolchain(tmp_path: Path) -> Path:
    """Install the scripted compiler in a private toolchain directory.

    The copy carries the running interpreter in its shebang rather than the
    ``#!/usr/bin/env python3`` the source file uses. The group runs on a
    private ``PATH`` -- the toolchain directory, ``/usr/bin`` and ``/bin`` --
    so an interpreter installed anywhere else, as a ``uv``-managed or Nix
    environment supplies, would be unreachable: the compile would fail for a
    host reason rather than a kache one.

    Returns
    -------
    Path
        The directory holding the ``rustc`` the composed group runs.
    """
    toolchain_bin = tmp_path / "toolchain" / "bin"
    toolchain_bin.mkdir(parents=True)
    compiler = toolchain_bin / "rustc"
    source = Path(__file__).with_name("fake_rustc.py").read_text(encoding="utf-8")
    _, _, body = source.partition("\n")
    compiler.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    compiler.chmod(0o755)
    return toolchain_bin


def _settings(
    executable: str, environment: dict[str, str], tmp_path: Path
) -> SupervisorConfig:
    """Build the kache-mode configuration naming the private deployment.

    Parameters
    ----------
    executable : str
        The kache client the group composes with.
    environment : dict[str, str]
        The private environment, naming the daemon socket and the store.
    tmp_path : Path
        The test's private directory.

    Returns
    -------
    SupervisorConfig
        A configuration whose health probe, composed group and event reader
        all name the one private daemon and store.
    """
    return SupervisorConfig(
        cache_mode="kache",
        kache_executable=executable,
        kache_socket=Path(environment["KACHE_SOCKET_PATH"]),
        kache_cache_dir=Path(environment["KACHE_CACHE_DIR"]),
        socket_path=tmp_path / "supervisor.sock",
        lock_path=tmp_path / "supervisor.lock",
        recovery_marker=tmp_path / "cache-recovery",
    )


def _argv(source: Path, output_dir: Path, metadata: str) -> tuple[str, ...]:
    """Build the Cargo-shaped library request a cached group composes.

    It names both ``link`` and ``dep-info`` because that is what a Cargo
    invocation asks for, and because kache stores a compilation only when the
    compiler writes every emit kind requested: a single-emit request is
    recorded as ``missing-output`` rather than stored, which would make the
    miss and the replay indistinguishable.

    Returns
    -------
    tuple[str, ...]
        The bare compiler vector, executable first.
    """
    return (
        "rustc",
        "--crate-name",
        "fixture",
        "--crate-type=rlib",
        "--edition=2021",
        "--emit=link,dep-info",
        "-C",
        f"metadata={metadata}",
        "--out-dir",
        str(output_dir),
        str(source),
    )


def _compile(
    spec: LaunchSpec, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run one composed group with exactly the environment it was given.

    The composed environment is a policy, not a suggestion: running the vector
    under this process's own environment would exercise whatever kache
    settings happen to be exported rather than the ones the supervisor
    composed.

    Parameters
    ----------
    spec : LaunchSpec
        The composed group.
    environment : dict[str, str]
        The private environment, supplying what composition did not set.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The client's captured output and exit status.
    """
    assert not [key for key in spec.env if key.startswith("SCCACHE_")], (
        "a cached kache group must not carry sccache settings"
    )
    return subprocess.run(
        spec.argv,
        check=False,
        capture_output=True,
        text=True,
        env={**environment, **spec.env},
        cwd=spec.cwd,
        timeout=300,
    )


def _artefacts(output_dir: Path) -> dict[str, bytes]:
    """Read every artefact the compiler wrote, for a cold/warm comparison.

    Returns
    -------
    dict[str, bytes]
        Each artefact's name and bytes.
    """
    return {path.name: path.read_bytes() for path in sorted(output_dir.iterdir())}


def test_real_kache_serves_a_health_checked_cacheable_compilation(
    tmp_path: Path,
) -> None:
    """A real kache deployment caches a group the supervisor composed."""
    executable = shutil.which("kache")
    assert executable is not None, "kache_host_marks must check binary availability"
    toolchain_bin = _toolchain(tmp_path)
    source = tmp_path / "fixture.rs"
    source.write_text("pub fn fixture() -> usize { 42 }\n", encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    environment = _base_environment(tmp_path, toolchain_bin)
    settings = _settings(executable, environment, tmp_path)
    backend = Kache(
        settings.kache_executable, settings.kache_socket, settings.kache_cache_dir
    )
    supervisor = Supervisor(settings, UserManager(_empty_runner), clock=lambda: 0.0)
    try:
        with _private_daemon(
            executable, environment, tmp_path / "kache-daemon.log"
        ) as daemon:
            assert backend.is_reachable() is True, "health must see the running daemon"
            supervisor.server.refresh_cache_mode()
            assert supervisor.status()["caching"] is True, (
                "the supervisor's cache reading must follow health"
            )

            spec = supervisor.groups.launch_spec(
                launch_pending(_argv(source, output_dir, "cold-warm"), environment),
                cached=True,
            )
            cold = _compile(spec, environment)
            assert cold.returncode == 0, cold.stderr
            cold_artefacts = _artefacts(output_dir)
            assert set(cold_artefacts) == {"libfixture.d", "libfixture.rlib"}, (
                "the group must write every artefact the request emitted"
            )

            warm = _compile(spec, environment)
            assert warm.returncode == 0, warm.stderr
            assert (warm.stdout, warm.stderr) == (cold.stdout, cold.stderr), (
                "a cache hit must not change the client's output"
            )
            assert _artefacts(output_dir) == cold_artefacts, (
                "a cache hit must restore the cold artefacts byte for byte"
            )

            store = Path(environment["KACHE_CACHE_DIR"])
            events = _read_events(store)
            assert [
                (row.get("result"), row.get("compiler_runs")) for row in events
            ] == [
                ("miss", 1),
                ("local_hit", 0),
            ], f"the store must record a miss then a compiler-free hit: {events!r}"

            summary = _read_summary(executable, environment)
            assert summary["misses"] == 1, "one request missed"
            assert summary["local_hits"] == 1, "one request was served locally"
            assert summary["total_crates"] == 2, "both requests reached the store"
            assert summary["skipped"] == 0, (
                "a skipped request is not a cached compilation"
            )

            assert daemon.poll() is None, "the daemon must outlive its clients"
        assert daemon.returncode is not None, "the private daemon did not stop"
    finally:
        supervisor.close()

    assert backend.is_reachable() is False, (
        "health must read a stopped daemon as unavailable"
    )
