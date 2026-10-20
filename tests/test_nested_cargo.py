"""Offline nested Cargo builds retain shared-cache and compiler admission controls."""

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from harness_processes import manager_nofile_limit
from systemd_harness import Harness, user_manager_available, wait_for

from buildlimits import adapter, config

pytestmark = pytest.mark.skipif(
    not user_manager_available(), reason="needs a systemd user manager"
)


_BUILD_SCRIPT = """use std::process::Command;
fn main() {
    let status = Command::new(std::env::var("NESTED_CARGO").unwrap())
        .args(["build", "--offline", "--manifest-path"])
        .arg(std::env::var("NESTED_MANIFEST").unwrap())
        .env("CARGO_TARGET_DIR", std::env::var("NESTED_TARGET").unwrap())
        .status().unwrap();
    assert!(status.success(), "nested Cargo build failed");
    println!("cargo:rerun-if-changed=build.rs");
}
"""


def _crate(directory: Path, name: str, *, nested: bool = False) -> Path:
    """Create a dependency-free library whose optional build script runs Cargo."""
    (directory / "src").mkdir(parents=True)
    (directory / "Cargo.toml").write_text(
        f'[package]\nname = "{name}"\nversion = "0.0.0"\nedition = "2021"\n'
    )
    (directory / "src/lib.rs").write_text("pub fn value() -> u8 { 1 }\n")
    if nested:
        (directory / "build.rs").write_text(_BUILD_SCRIPT)
    return directory / "Cargo.toml"


def _launcher(
    path: Path, environment: dict[str, str], cargo: str, manifest: Path
) -> None:
    """Write an outer admitted Python launcher without altering Rust wrappers."""
    # Global admission admits this non-Rust parent without sccache. Cargo's wrappers
    # then contact the isolated supervisor while this outer token stays held.
    selected = {
        key: value
        for key, value in environment.items()
        if key.startswith("BUILD_LIMITS_")
        or key.startswith("NESTED_")
        or key in {"CARGO_TARGET_DIR", "PYTHONPATH"}
    }
    path.write_text(
        "import json, os, subprocess, sys\n"
        f"os.environ.update(json.loads({json.dumps(selected)!r}))\n"
        f"sys.exit(subprocess.call({[cargo, 'build', '--offline', '--jobs', '1', '--manifest-path', str(manifest)]!r}))\n"
    )


def _require_managed_admission() -> None:
    """Skip before fixture startup when production compiler admission is unavailable."""
    wrapper = shutil.which(os.environ.get("RUSTC_WRAPPER", ""))
    if wrapper is None or Path(wrapper).resolve().name != "build-limits-rustc":
        pytest.skip(
            "managed admission wrapper unavailable; admission cannot be bypassed"
        )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.connect(str(config.supervisor_socket(os.environ)))
    except OSError:
        pytest.skip("production admission socket unavailable; not acceptance evidence")


@pytest.mark.timeout(1800)
@pytest.mark.systemd_user
def test_offline_nested_cargo_preserves_admission_and_recovers_baseline() -> None:
    """Real nested Cargo succeeds with one slot and leaves no tokens or queued FDs."""
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("Cargo unavailable; this skip is not issue255 acceptance evidence")
    _require_managed_admission()
    with contextlib.ExitStack() as owned:
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        targets = Path(
            owned.enter_context(
                tempfile.TemporaryDirectory(prefix="issue255-cargo-target-", dir=cache)
            )
        )
        harness = Harness(1, nofile=(128, manager_nofile_limit()))
        owned.callback(harness.close)
        harness.start()
        baseline = harness.status()["descriptors"]["open"]
        outer = _crate(harness.work / "outer", "outer_pressure", nested=True)
        inner = _crate(harness.work / "inner", "inner_pressure")
        environment = {
            **harness.env,
            "NESTED_CARGO": cargo,
            "NESTED_MANIFEST": str(inner),
            "NESTED_TARGET": str(targets / "inner"),
            "CARGO_TARGET_DIR": str(targets / "outer"),
            # The installed wrapper executable must load the revision under test.
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    (
                        str(Path(adapter.__file__).resolve().parent.parent),
                        os.environ.get("PYTHONPATH", ""),
                    ),
                )
            ),
        }
        launcher = harness.work / "launch_cargo.py"
        _launcher(launcher, environment, cargo, outer)
        # Keep RUSTC_WRAPPER/RUSTDOC and the default/shared CARGO_HOME intact.
        outer_admission = (
            "import sys; from buildlimits.adapter import _main; "
            f"sys.exit(_main([sys.executable, {str(launcher)!r}], cacheable=False))"
        )
        process = owned.enter_context(
            subprocess.Popen(
                [sys.executable, "-c", outer_admission],
                cwd=harness.work,
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        )
        try:
            output, _stderr = process.communicate(timeout=1800)
            assert process.returncode == 0, output.decode(errors="replace")
            _assert_build_recovered(harness, targets, baseline)
        finally:
            if process.poll() is None:
                # Stop only this harness's compiler scopes before returning
                # its outer production token; no unrelated process is touched.
                harness.close()
                process.kill()
                process.wait()


def _assert_build_recovered(harness: Harness, targets: Path, baseline: int) -> None:
    """Verify both actual compilations and complete isolated admission recovery."""
    assert (targets / "inner/debug/libinner_pressure.rlib").exists(), (
        "the nested build actually compiled"
    )
    assert (targets / "outer/debug/libouter_pressure.rlib").exists(), (
        "the outer build actually compiled"
    )
    assert wait_for(lambda: harness.status()["held"] == 0), harness.journal()
    status = harness.status()
    assert status["healthy"] and not status["queued"], status
    assert status["descriptors"]["retained"] == 0, (
        "completed Cargo requests retain no rights"
    )
    assert status["descriptors"]["open"] == baseline, (
        "supervisor descriptors recover exactly"
    )
    assert status["descriptors"]["high_water"] <= status["descriptors"]["budget"], (
        status
    )
    print(
        "issue255 nested Cargo evidence: "
        + json.dumps(
            {
                "baseline_count": baseline,
                "final_count": status["descriptors"]["open"],
                "high_water": status["descriptors"]["high_water"],
                "budget": status["descriptors"]["budget"],
                "held": status["held"],
                "retained": status["descriptors"]["retained"],
                "limits": [
                    status["descriptors"]["soft"],
                    status["descriptors"]["hard"],
                ],
            },
            sort_keys=True,
        )
    )
