"""Opt-in acceptance checks for the supported sccache server binary."""

import collections.abc as cabc
import os
import shutil
import subprocess
import time
import typing as typ
from pathlib import Path

from sccache_host import (
    _WINDOW_COUNT,
    _base_environment,
    _compile_command,
    _private_server,
    _read_stats,
    _run_compile,
    _server_pid,
    sccache_host_marks,
)
from sccache_stats import (
    _assert_delta,
    _assert_windows_match,
    _record_window,
    _write_windows,
)

pytestmark = sccache_host_marks()


class _RefusalProbe(typ.NamedTuple):
    """Describe a local refusal-counter case for the opt-in acceptance test."""

    arguments: tuple[str, ...]
    name: str
    truth: typ.Literal["declined", "not_compile"]
    expected: dict[str, int]


class _CacheMissProbe(typ.NamedTuple):
    """Collect one miss request and its replay-window label."""

    source: Path
    output: Path
    metadata: str
    label: str


class _ChildCompileProbe(typ.NamedTuple):
    """Describe one compiler-child observation request."""

    source: Path
    output: Path
    metadata: str


def _server_cgroup_pids(server_pid: int) -> tuple[int, ...]:
    """Read the server's cgroup-v2 process list for live child inspection."""
    cgroup_lines = Path(f"/proc/{server_pid}/cgroup").read_text(encoding="utf-8")
    unified_path = next(
        (line.partition("::")[2] for line in cgroup_lines.splitlines() if "::" in line),
        None,
    )
    if unified_path is None:
        message = f"server {server_pid} has no cgroup-v2 membership"
        raise AssertionError(message)
    process_file = Path("/sys/fs/cgroup") / unified_path.lstrip("/") / "cgroup.procs"
    return tuple(
        int(line)
        for line in process_file.read_text(encoding="ascii").splitlines()
        if line.isdigit()
    )


def _compiler_arguments(server_pid: int, metadata: str) -> list[tuple[str, ...]]:
    """Return live compiler argv vectors in the server's cgroup for one key."""
    matches: list[tuple[str, ...]] = []
    for process_id in _server_cgroup_pids(server_pid):
        try:
            raw_arguments = Path(f"/proc/{process_id}/cmdline").read_bytes()
        except OSError:
            continue
        arguments = tuple(
            part.decode("utf-8", errors="replace")
            for part in raw_arguments.rstrip(b"\0").split(b"\0")
            if part
        )
        # The client carries the original emit flags, not the work it spawned.
        if arguments and Path(arguments[0]).name == "sccache":
            continue
        is_filename_probe = any(
            argument == "--print=file-names"
            or (
                argument == "--print"
                and index + 1 < len(arguments)
                and arguments[index + 1] == "file-names"
            )
            for index, argument in enumerate(arguments)
        )
        if is_filename_probe:
            continue
        if any("metadata=" + metadata in argument for argument in arguments):
            matches.append(arguments)
    return matches


def _emit_values(arguments: cabc.Sequence[str]) -> frozenset[str]:
    """Return emit kinds from a live compiler argv vector."""
    for index, argument in enumerate(arguments):
        if argument.startswith("--emit="):
            return frozenset(argument.partition("=")[2].split(","))
        if argument == "--emit" and index + 1 < len(arguments):
            return frozenset(arguments[index + 1].split(","))
    return frozenset()


def _sample_compiler_arguments(
    server_pid: int,
    metadata: str,
    process: subprocess.Popen[str],
) -> list[tuple[str, ...]]:
    """Sample matching compiler argv until the client request exits."""
    samples: list[tuple[str, ...]] = []
    deadline = time.monotonic() + 15
    while process.poll() is None and time.monotonic() < deadline:
        samples.extend(_compiler_arguments(server_pid, metadata))
        time.sleep(0.01)
    return samples


def _run_and_sample_child(
    executable: str,
    environment: dict[str, str],
    server_pid: int,
    probe: _ChildCompileProbe,
) -> list[tuple[str, ...]]:
    """Run a compile request and return its sampled compiler arguments."""
    with subprocess.Popen(
        _compile_command(executable, probe.source, probe.output, probe.metadata),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        samples = _sample_compiler_arguments(server_pid, probe.metadata, process)
        try:
            _stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        assert process.returncode == 0, stderr
    return samples


def _assert_server_identity(server_pid: int, scoped: bool) -> None:
    """Check that the private socket belongs to the expected server process."""
    assert server_pid != os.getpid(), "SO_PEERCRED resolved to the test process"
    server_arguments = Path(f"/proc/{server_pid}/cmdline").read_bytes()
    assert b"sccache" in server_arguments, (
        f"SO_PEERCRED peer {server_pid} does not look like an sccache server"
    )
    if scoped:
        server_environment = Path(f"/proc/{server_pid}/environ").read_bytes()
        assert any(
            entry.startswith(b"INVOCATION_ID=")
            for entry in server_environment.split(b"\0")
        ), f"systemd-scoped server {server_pid} has no invocation ID"


def _check_cache_miss(
    executable: str,
    environment: dict[str, str],
    probe: _CacheMissProbe,
    windows: list[dict[str, object]],
) -> subprocess.CompletedProcess[str]:
    """Run one expected cache miss and record its counter window."""
    before = _read_stats(executable, environment)
    result = _run_compile(
        _compile_command(executable, probe.source, probe.output, probe.metadata),
        environment,
    )
    after = _read_stats(executable, environment)
    assert result.returncode == 0, result.stderr
    _assert_delta(
        before,
        after,
        {
            "compile_requests": 1,
            "requests_executed": 1,
            "cache_misses": 1,
            "compilations": 1,
        },
    )
    _record_window(windows, probe.label, "miss", (before, after))
    return result


def _check_cold_and_warm(
    executable: str,
    environment: dict[str, str],
    source: Path,
    windows: list[dict[str, object]],
) -> None:
    """Check miss and hit footprints plus client output parity."""
    output = source.parent / "out" / "libfixture.rlib"
    cold = _check_cache_miss(
        executable,
        environment,
        _CacheMissProbe(source, output, "a1-shared", "cold-miss"),
        windows,
    )
    cold_artefact = output.read_bytes()

    before_warm = _read_stats(executable, environment)
    warm = _run_compile(
        _compile_command(executable, source, output, "a1-shared"), environment
    )
    after_warm = _read_stats(executable, environment)
    assert (warm.returncode, warm.stdout, warm.stderr) == (
        cold.returncode,
        cold.stdout,
        cold.stderr,
    ), "warm cache hit changed the client return code or output"
    assert output.read_bytes() == cold_artefact, (
        "cache-hit artefact differs from cold output"
    )
    _assert_delta(
        before_warm,
        after_warm,
        {"compile_requests": 1, "requests_executed": 1, "cache_hits": 1},
    )
    _record_window(windows, "warm-hit", "hit", (before_warm, after_warm))


def _check_refusal_probe(
    executable: str,
    environment: dict[str, str],
    windows: list[dict[str, object]],
    probe: _RefusalProbe,
) -> None:
    """Check one probe's refusal counter and record its truth window."""
    before = _read_stats(executable, environment)
    result = subprocess.run(
        [executable, *probe.arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )
    after = _read_stats(executable, environment)
    assert result.returncode == 0, result.stderr
    _assert_delta(before, after, probe.expected)
    _record_window(windows, probe.name, probe.truth, (before, after))


def _check_post_exit_readings(
    executable: str,
    environment: dict[str, str],
    tmp_path: Path,
    windows: list[dict[str, object]],
) -> None:
    """Check 200 readings immediately after controlled cache misses."""
    source = tmp_path / "fixture.rs"
    for index in range(_WINDOW_COUNT):
        probe = _CacheMissProbe(
            source,
            tmp_path / "windows" / f"{index:03d}" / "libfixture.rlib",
            f"a2-window-{index:03d}",
            f"a2-{index:03d}",
        )
        _check_cache_miss(
            executable,
            environment,
            probe,
            windows,
        )


def _check_child_arguments(
    executable: str,
    environment: dict[str, str],
    server_pid: int,
    tmp_path: Path,
) -> None:
    """Observe full emit on a miss and dep-info-only work on a hit."""
    source = tmp_path / "fixture.rs"
    metadata = "a9-cold"
    cold_samples = _run_and_sample_child(
        executable,
        environment,
        server_pid,
        _ChildCompileProbe(
            source, tmp_path / "a9" / "cold" / "libfixture.rlib", metadata
        ),
    )
    assert any("link" in _emit_values(arguments) for arguments in cold_samples), (
        f"no full-emit compiler child observed; samples={cold_samples!r}"
    )

    warm_samples = _run_and_sample_child(
        executable,
        environment,
        server_pid,
        _ChildCompileProbe(
            source, tmp_path / "a9" / "warm" / "libfixture.rlib", metadata
        ),
    )
    assert warm_samples, "warm request did not expose its hash-key compiler pass"
    assert all("link" not in _emit_values(arguments) for arguments in warm_samples), (
        f"warm request had a full-emit compiler child; samples={warm_samples!r}"
    )
    assert all("dep-info" in _emit_values(arguments) for arguments in warm_samples), (
        f"warm compiler child had unexpected emit args; samples={warm_samples!r}"
    )


def test_real_sccache_014_counter_and_child_contracts(tmp_path: Path) -> None:
    """Check 0.14.0 counter footprints and compiler-child arguments."""
    executable = shutil.which("sccache")
    assert executable is not None, "sccache_host_marks must check binary availability"
    toolchain_bin = tmp_path / "toolchain" / "bin"
    toolchain_bin.mkdir(parents=True)
    fake_compiler = toolchain_bin / "rustc"
    shutil.copy2(Path(__file__).with_name("fake_rustc.py"), fake_compiler)
    fake_compiler.chmod(0o755)
    source = tmp_path / "fixture.rs"
    source.write_text("pub fn fixture() -> usize { 42 }\n", encoding="utf-8")
    environment = _base_environment(tmp_path, toolchain_bin)
    windows: list[dict[str, object]] = []

    with _private_server(executable, environment, tmp_path / "sccache-server.log") as (
        server_process,
        scoped,
    ):
        server_pid = _server_pid(environment)
        _assert_server_identity(server_pid, scoped)
        _check_cold_and_warm(executable, environment, source, windows)
        for probe in (
            _RefusalProbe(
                ("rustc", "-vV"),
                "version-probe",
                "declined",
                {"compile_requests": 1, "requests_not_cacheable": 1},
            ),
            _RefusalProbe(
                ("rustc", "--print=sysroot"),
                "sysroot-probe",
                "not_compile",
                {"compile_requests": 1, "requests_not_compile": 1},
            ),
        ):
            _check_refusal_probe(executable, environment, windows, probe)
        _check_post_exit_readings(executable, environment, tmp_path, windows)
        _check_child_arguments(executable, environment, server_pid, tmp_path)

    assert server_process.returncode is not None, "private sccache server did not stop"
    assert len(windows) == _WINDOW_COUNT + 4, (
        f"expected {_WINDOW_COUNT + 4} replay windows, got {len(windows)}"
    )
    if os.environ.get("BUILD_LIMITS_SCCACHE_REGENERATE_FIXTURE") == "1":
        _write_windows(windows)
    else:
        _assert_windows_match(windows)
