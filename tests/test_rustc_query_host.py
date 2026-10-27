"""Cargo host acceptance for rustc query admission under held slots."""

import collections.abc as cabc
import contextlib
import shutil
import typing as typ
from pathlib import Path

import pytest
from rustc_linter_acceptance import LinterAcceptance, run_linter_acceptance
from rustc_query_host_checks import (
    finish_barriers,
    finish_or_fail,
    matching_dylint_driver,
    tool_version,
)
from rustc_query_host_observations import record_observation, result_evidence
from rustc_query_host_support import (
    ObservedCommand,
    caller_dylint_library,
    cargo_environment,
    dependency_free_crate,
    hold_pool,
    require_host_command,
    start_observed,
)
from rustc_query_probe_records import (
    is_target_info_query,
    is_version_query,
    timing_entries,
)
from rustc_query_timing_shim import install_timing_shim
from systemd_harness import Harness, systemd_user_marks, wait_for

if typ.TYPE_CHECKING:
    from rustc_query_host_support import ProcessResult
    from rustc_query_probe_records import ProbeRecord

pytestmark = systemd_user_marks("needs a systemd user manager and host Cargo tools")


@pytest.fixture(scope="module")
def cargo_path() -> Path:
    """Skip host Cargo acceptance when Cargo is absent."""
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("Cargo unavailable; host query acceptance requires Cargo")
    return Path(cargo)


def _clippy_driver() -> str:
    """Require Cargo's Clippy command and its installed driver."""
    require_host_command("cargo-clippy", "cargo-clippy command absent from PATH")
    driver = shutil.which("clippy-driver")
    if driver is None:
        pytest.skip("Clippy driver absent from PATH")
    return driver


def _assert_metadata_probes_complete(
    result: ProcessResult,
    entries: list[ProbeRecord],
    harness: Harness,
    command: list[str],
) -> None:
    """Require metadata probes to finish outside the held compiler pool."""
    version = [entry for entry in entries if is_version_query(entry)]
    target_info = [entry for entry in entries if is_target_info_query(entry)]
    assert result.status == 0, result_evidence(result, entries)
    assert version and all(entry["status"] == 0 for entry in version), entries
    assert target_info and all(entry["status"] == 0 for entry in target_info), entries
    assert "--no-deps" not in command, "metadata must resolve the full graph"
    status = harness.status()
    assert status["held"] == harness.slots and not status["queued"], status


def test_cold_cargo_metadata_finishes_while_every_slot_is_held(
    cargo_path: Path,
    record_property: cabc.Callable[[str, object], None],
) -> None:
    """Complete real metadata probes without entering the full supervisor queue."""
    record_property(
        "cargo_metadata_cargo_version",
        tool_version([str(cargo_path), "--version", "--verbose"], "Cargo"),
    )
    record_property(
        "cargo_metadata_rustc_version",
        tool_version(["rustc", "--version", "--verbose"], "rustc"),
    )
    with contextlib.ExitStack() as owned:
        harness = Harness(1)
        owned.callback(harness.close)
        harness.start()
        shim, timing_log = install_timing_shim(harness)
        manifest = dependency_free_crate(harness.work / "metadata", "metadata_probe")
        environment = cargo_environment(harness, shim, timing_log)
        release, barriers = hold_pool(owned, harness)
        command = [
            str(cargo_path),
            "metadata",
            "--offline",
            "--format-version",
            "1",
            "--manifest-path",
            str(manifest),
        ]
        observed = start_observed(
            owned,
            ObservedCommand(
                argv=command,
                cwd=manifest.parent,
                environment=environment,
                stdout_path=harness.work / "metadata.json",
            ),
        )
        result = finish_or_fail(observed, 120, harness, timing_log)
        entries = timing_entries(timing_log)
        _assert_metadata_probes_complete(result, entries, harness, command)
        record_observation(record_property, "cargo_metadata", result, entries)

        release.touch()
        finish_barriers(barriers)
        assert wait_for(
            lambda: harness.status()["held"] == 0 and not harness.status()["queued"],
            timeout=60,
        ), harness.journal()


@pytest.mark.timeout(900)
def test_clippy_resolution_and_second_metadata_pass_while_compile_waits(
    cargo_path: Path,
    record_property: cabc.Callable[[str, object], None],
) -> None:
    """Let Clippy resolve and run its probes while a real compile queues."""
    driver = _clippy_driver()
    versions = {
        "cargo_version": tool_version(
            [str(cargo_path), "--version", "--verbose"], "Cargo"
        ),
        "cargo_clippy_version": tool_version(
            [str(cargo_path), "clippy", "--version"], "Cargo Clippy"
        ),
        "driver_version": tool_version([driver, "--version"], "clippy-driver"),
        "rustc_version": tool_version(["rustc", "--version", "--verbose"], "rustc"),
    }
    run_linter_acceptance(
        LinterAcceptance(
            cargo=cargo_path,
            name="clippy",
            driver_name="clippy-driver",
            command_prefix=["clippy", "--offline"],
            command_suffix=[],
            tool_versions=versions,
            extra_environment=None,
            toolchain=None,
            queue_timeout=180,
            completion_timeout=240,
            record_property=record_property,
        ),
    )


@pytest.mark.timeout(1200)
def test_dylint_resolution_and_second_metadata_pass_while_compile_waits(
    cargo_path: Path,
    record_property: cabc.Callable[[str, object], None],
) -> None:
    """Resolve Dylint metadata and probes before its one real compile queues."""
    require_host_command("cargo-dylint", "cargo-dylint command absent from PATH")
    cargo_version = tool_version([str(cargo_path), "--version", "--verbose"], "Cargo")
    dylint_version = tool_version(
        [str(cargo_path), "dylint", "--version"], "Cargo Dylint"
    )
    record_property("dylint_cargo_version", cargo_version)
    record_property("dylint_cargo_plugin_version", dylint_version)
    library_path, library = caller_dylint_library()
    driver, toolchain, driver_root = matching_dylint_driver(library)
    versions = {
        "cargo_version": cargo_version,
        "cargo_dylint_version": dylint_version,
        "driver_version": tool_version([str(driver), "--version"], "dylint-driver"),
        "rustc_version": tool_version(
            ["rustup", "run", toolchain, "rustc", "--version", "--verbose"],
            "Dylint toolchain rustc",
        ),
    }
    run_linter_acceptance(
        LinterAcceptance(
            cargo=cargo_path,
            name="dylint",
            driver_name="dylint-driver",
            command_prefix=["dylint", "--all"],
            command_suffix=["--", "--offline"],
            tool_versions=versions,
            extra_environment={
                "DYLINT_DRIVER_PATH": str(driver_root),
                "DYLINT_LIBRARY_PATH": library_path,
            },
            toolchain=toolchain,
            queue_timeout=240,
            completion_timeout=300,
            record_property=record_property,
        ),
    )
