"""Measure real rustc and workspace-driver query latency outside admission."""

import collections.abc as cabc
import contextlib
import dataclasses as dc
import datetime as dt
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from rustc_query_host_checks import (
    _matching_dylint_driver,
    _version,
)
from rustc_query_host_support import (
    CLEAN_COMPILER_ENVIRONMENT,
    caller_dylint_library,
    hold_pool,
)
from rustc_query_support import cargo_probe
from systemd_harness import Harness, systemd_user_marks

pytestmark = systemd_user_marks("needs a systemd user manager and Rust toolchains")

_SAMPLES = 5
_SAMPLE_TIMEOUT = 10
_QUERY_FORMS = {
    "version": ["-vV"],
    "cargo_target_info": cargo_probe(),
}


@dc.dataclass(frozen=True, slots=True)
class TimingScenario:
    """Commands and evidence recorder for one full held-pool timing run."""

    harness: Harness
    commands: cabc.Mapping[str, cabc.Sequence[str]]
    environment: cabc.Mapping[str, str]
    record_property: cabc.Callable[[str, object], None]
    versions: cabc.Mapping[str, str]


@dc.dataclass(frozen=True, slots=True)
class ProbeRun:
    """One query sample and the held-pool state it must preserve."""

    harness: Harness
    label: str
    query_name: str
    argv: list[str]
    environment: cabc.Mapping[str, str]


def test_rustc_and_clippy_queries_finish_below_one_second(
    record_property: cabc.Callable[[str, object], None],
) -> None:
    """Keep every token held while direct rustc and Clippy probes are timed."""
    rustc = _rustc_from_sysroot(None)
    clippy_driver = shutil.which("clippy-driver")
    if clippy_driver is None:
        pytest.skip("Clippy driver absent from PATH")
    rustc_version = _version([str(rustc), "--version", "--verbose"], "rustc")
    clippy_version = _version([clippy_driver, "--version"], "clippy-driver")
    _run_timing_suite(
        commands={
            "rustc": (str(rustc),),
            "clippy-driver": (clippy_driver, str(rustc)),
        },
        environment_overrides={},
        versions={"rustc": rustc_version, "clippy_driver": clippy_version},
        record_property=record_property,
    )


def test_dylint_queries_finish_below_one_second(
    record_property: cabc.Callable[[str, object], None],
) -> None:
    """Measure an installed Dylint driver with its caller-supplied library."""
    library_path, library = caller_dylint_library()
    driver, toolchain, _driver_root = _matching_dylint_driver(library)
    rustc = _rustc_from_sysroot(toolchain)
    driver_version = _version([str(driver), "--version"], "dylint-driver")
    rustc_version = _version(
        ["rustup", "run", toolchain, "rustc", "--version", "--verbose"],
        "Dylint toolchain rustc",
    )

    _run_timing_suite(
        commands={"dylint-driver": (str(driver), str(rustc))},
        environment_overrides={
            "DYLINT_LIBRARY_PATH": library_path,
            "DYLINT_LIBS": json.dumps([str(library)]),
        },
        versions={
            "dylint_driver": driver_version,
            "dylint_toolchain_rustc": rustc_version,
        },
        record_property=record_property,
    )


def _run_timing_suite(
    *,
    commands: cabc.Mapping[str, cabc.Sequence[str]],
    environment_overrides: cabc.Mapping[str, str],
    versions: cabc.Mapping[str, str],
    record_property: cabc.Callable[[str, object], None],
) -> None:
    """Measure a command matrix while each supervisor token stays held."""
    with contextlib.ExitStack() as owned:
        harness = Harness(1)
        owned.callback(harness.close)
        harness.start()
        release, barriers = hold_pool(owned, harness)
        environment = _clean_environment(harness.env)
        environment.update(environment_overrides)
        _record_timing_context(record_property, versions)
        measurements = _measure_with_evidence(
            TimingScenario(
                harness=harness,
                commands=commands,
                environment=environment,
                record_property=record_property,
                versions=versions,
            )
        )
        _record_timing_properties(record_property, measurements)
        release.touch()
        _finish_barriers(barriers)
        status = harness.status()
        assert status["held"] == 0 and not status["queued"], status


def _rustc_from_sysroot(toolchain: str | None) -> Path:
    """Resolve the real compiler binary through its selected sysroot."""
    if toolchain is None and shutil.which("rustc") is None:
        pytest.skip("rustc absent from PATH")
    command = (
        ["rustc", "--print", "sysroot"]
        if toolchain is None
        else ["rustup", "run", toolchain, "rustc", "--print", "sysroot"]
    )
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=_clean_environment(os.environ),
        timeout=30,
    )
    assert result.returncode == 0, f"rustc sysroot lookup failed: {result.stderr}"
    rustc = Path(result.stdout.strip()) / "bin" / "rustc"
    assert rustc.is_file(), f"sysroot rustc is missing: {rustc}"
    return rustc


def _clean_environment(environment: cabc.Mapping[str, str]) -> dict[str, str]:
    """Remove ambient compiler and flag overrides from direct probe runs."""
    cleaned = dict(environment)
    for name in CLEAN_COMPILER_ENVIRONMENT:
        cleaned.pop(name, None)
    cleaned.pop("RUSTUP_TOOLCHAIN", None)
    return cleaned


def _measure_commands(scenario: TimingScenario) -> dict[str, list[dict[str, object]]]:
    """Warm and sample each query form while the pool remains fully occupied."""
    measurements: dict[str, list[dict[str, object]]] = {}
    for label, prefix in scenario.commands.items():
        for query_name, query in _QUERY_FORMS.items():
            argv = [*prefix, *query]
            probe = ProbeRun(
                scenario.harness,
                label,
                query_name,
                argv,
                scenario.environment,
            )
            _run_probe(probe)
            samples: list[dict[str, object]] = []
            for _ in range(_SAMPLES):
                sample = _run_probe(probe)
                samples.append(sample)
                _record_json_property(
                    scenario.record_property,
                    f"{label}_{query_name}_sample_{len(samples)}",
                    sample,
                )
            measurements[f"{label}_{query_name}"] = samples
    return measurements


def _measure_with_evidence(
    scenario: TimingScenario,
) -> dict[str, list[dict[str, object]]]:
    """Add selected versions to any query timing failure report."""
    try:
        return _measure_commands(scenario)
    except (AssertionError, subprocess.TimeoutExpired) as error:
        raise AssertionError(
            f"timed query failed: {error}; versions={scenario.versions}"
        ) from error


def _run_probe(probe: ProbeRun) -> dict[str, object]:
    """Run one bounded raw query and confirm no slot was released or queued."""
    started = time.monotonic()
    try:
        result = subprocess.run(
            probe.argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=probe.environment,
            check=False,
            timeout=_SAMPLE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError(
            f"{probe.label} {probe.query_name} exceeded {_SAMPLE_TIMEOUT}s: "
            f"{probe.argv}; "
            f"stdout={error.stdout!r}; stderr={error.stderr!r}"
        ) from error
    elapsed = time.monotonic() - started
    assert result.returncode == 0, (
        probe.label,
        probe.query_name,
        probe.argv,
        result.stdout,
        result.stderr,
    )
    assert elapsed < 1.0, (
        f"{probe.label} {probe.query_name} took {elapsed:.6f}s: {probe.argv}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    status = probe.harness.status()
    assert status["held"] == probe.harness.slots and not status["queued"], status
    return {
        "argv": probe.argv,
        "duration_seconds": round(elapsed, 6),
        "status": result.returncode,
    }


def _record_timing_properties(
    record_property: cabc.Callable[[str, object], None],
    measurements: dict[str, list[dict[str, object]]],
) -> None:
    """Record the successful sample matrix."""
    _record_json_property(record_property, "rustc_query_timing_samples", measurements)


def _record_json_property(
    record_property: cabc.Callable[[str, object], None], name: str, value: object
) -> None:
    """Serialize structured pytest evidence consistently as sorted JSON."""
    record_property(name, json.dumps(value, sort_keys=True))


def _record_timing_context(
    record_property: cabc.Callable[[str, object], None],
    versions: cabc.Mapping[str, str],
) -> None:
    """Record host provenance before measurements so failures retain it."""
    record_property("rustc_query_host", os.uname().nodename)
    record_property("rustc_query_date_utc", dt.datetime.now(dt.UTC).isoformat())
    for name, version in versions.items():
        record_property(f"rustc_query_version_{name}", version)
    _record_json_property(record_property, "rustc_query_tool_versions", versions)


def _finish_barriers(processes: list[subprocess.Popen[bytes]]) -> None:
    """Reap every released barrier compiler and require it to exit normally."""
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, (stdout, stderr)
