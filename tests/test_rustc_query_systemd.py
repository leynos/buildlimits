"""Prove safe rustc probes bypass a fully held real supervisor pool."""

import contextlib
import dataclasses as dc
import json
import subprocess
import time
from pathlib import Path

from harness_processes import own_process
from rustc_query_host_support import hold_pool
from rustc_query_support import temporary_executable
from systemd_harness import Harness, systemd_user_marks, wait_for

pytestmark = systemd_user_marks("needs a systemd user manager and busctl")

_FAKE_RUSTC = r"""#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

arguments = sys.argv[1:]
cgroup = Path("/proc/self/cgroup").read_text()
marker = os.environ.get("RUSTC_PROBE_MARKER")
if marker:
    Path(marker).write_text(json.dumps({"argv": arguments, "cgroup": cgroup}))

if "-vV" in arguments:
    print("rustc 1.99.0 (fake)")
    print("host: fake-host")
elif "--version" in arguments or "-V" in arguments:
    print("rustc 1.99.0 (fake)")
elif any(argument == "--emit" or argument.startswith("--emit=") for argument in arguments):
    release = os.environ.get("RUSTC_RELEASE_FILE")
    while release and not Path(release).exists():
        time.sleep(0.01)
"""

_FORWARDER = r"""#!/usr/bin/env python3
import os
import sys

os.execv(sys.argv[1], sys.argv[1:])
"""


@dc.dataclass(frozen=True, slots=True)
class HeldPoolScenario:
    """Fake compiler tools and barriers for one admission scenario."""

    harness: Harness
    release: Path
    rustc: Path
    clippy: Path
    dylint: Path
    other_wrapper: Path
    held_compilers: list[subprocess.Popen[bytes]]


def test_queries_bypass_a_full_pool_but_compiles_still_queue(tmp_path: Path) -> None:
    """Keep all tokens held while direct and recognized wrapped probes finish."""
    with contextlib.ExitStack() as owned:
        harness = Harness(2)
        owned.callback(harness.close)
        harness.start()
        scenario = _prepare_scenario(harness, owned, tmp_path)
        _assert_queries_are_exempt(owned, scenario)
        queued, markers = _queue_compile_and_unknown_wrapper(owned, scenario)
        _wait_for_both_queued(scenario)
        _release_and_verify(scenario, queued, markers)


def _prepare_scenario(
    harness: Harness, owned: contextlib.ExitStack, tmp_path: Path
) -> HeldPoolScenario:
    """Create fake tools, hold all tokens, and prove barriers are contained.

    Returns
    -------
    HeldPoolScenario
        The tools, pool barriers and release path for this test.
    """
    rustc = temporary_executable(tmp_path / "toolchain" / "bin" / "rustc", _FAKE_RUSTC)
    clippy = temporary_executable(tmp_path / "clippy" / "clippy-driver", _FORWARDER)
    dylint = temporary_executable(tmp_path / "dylint" / "dylint-driver", _FORWARDER)
    other_wrapper = temporary_executable(
        tmp_path / "other" / "other-wrapper", _FORWARDER
    )
    release, held = hold_pool(owned, harness)
    return HeldPoolScenario(
        harness, release, rustc, clippy, dylint, other_wrapper, held
    )


def _assert_queries_are_exempt(
    owned: contextlib.ExitStack, scenario: HeldPoolScenario
) -> None:
    """Require recognized direct and workspace-driver queries to bypass tokens."""
    queries = (
        ("rustc", (str(scenario.rustc), "-vV"), {}),
        ("clippy-driver", (str(scenario.clippy), str(scenario.rustc), "-vV"), {}),
        (
            "dylint-driver",
            (str(scenario.dylint), str(scenario.rustc), "-vV"),
            {"RUSTC_WORKSPACE_WRAPPER": str(scenario.dylint)},
        ),
    )
    for name, argv, extra_env in queries:
        marker = scenario.harness.work / f"{name}-query.json"
        process = own_process(
            owned,
            scenario.harness.invoke(
                argv,
                env={"RUSTC_PROBE_MARKER": str(marker), **extra_env},
                piped=True,
            ),
        )
        started = time.monotonic()
        stdout, stderr = process.communicate(timeout=2)
        elapsed = time.monotonic() - started
        assert process.returncode == 0, (stdout, stderr)
        assert elapsed < 2.0, f"{name} query took {elapsed:.3f}s"
        assert b"rustc 1.99.0" in stdout, (name, stdout, stderr)
        assert marker.exists(), f"{name} reached the fake rustc"
        cgroup = json.loads(marker.read_text())["cgroup"]
        assert scenario.harness.compilers_slice not in cgroup, cgroup
        status = scenario.harness.status()
        assert status["held"] == scenario.harness.slots and not status["queued"], status


def _queue_compile_and_unknown_wrapper(
    owned: contextlib.ExitStack, scenario: HeldPoolScenario
) -> tuple[list[subprocess.Popen[bytes]], list[Path]]:
    """Start one real compile and unknown wrapper, both expected to queue.

    Returns
    -------
    tuple[list[subprocess.Popen[bytes]], list[Path]]
        Queued adapter processes and their execution marker paths.
    """
    markers = [
        scenario.harness.work / "ordinary-compile.json",
        scenario.harness.work / "unknown-wrapper.json",
    ]
    processes = [
        own_process(
            owned,
            scenario.harness.invoke(
                (
                    str(scenario.rustc),
                    "--crate-name",
                    "ordinary",
                    "--emit=obj",
                ),
                env={
                    "RUSTC_PROBE_MARKER": str(markers[0]),
                    "RUSTC_RELEASE_FILE": str(scenario.release),
                },
                piped=True,
            ),
        ),
        own_process(
            owned,
            scenario.harness.invoke(
                (str(scenario.other_wrapper), str(scenario.rustc), "-vV"),
                env={"RUSTC_PROBE_MARKER": str(markers[1])},
                piped=True,
            ),
        ),
    ]
    return processes, markers


def _wait_for_both_queued(scenario: HeldPoolScenario) -> None:
    """Prove neither queued invocation ran before barriers are released."""
    marker_paths = [
        scenario.harness.work / "ordinary-compile.json",
        scenario.harness.work / "unknown-wrapper.json",
    ]
    assert wait_for(
        lambda: len(scenario.harness.status()["queued"]) == 2, timeout=30
    ), scenario.harness.journal()
    assert not any(marker.exists() for marker in marker_paths), (
        "neither queued compiler has started"
    )


def _release_and_verify(
    scenario: HeldPoolScenario,
    queued: list[subprocess.Popen[bytes]],
    markers: list[Path],
) -> None:
    """Release tokens, reap every process, and verify queued cgroup execution."""
    scenario.release.touch()
    for process in (*scenario.held_compilers, *queued):
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, (stdout, stderr)
    status = scenario.harness.status()
    assert status["held"] == 0 and not status["queued"] and status["healthy"], status
    assert all(marker.exists() for marker in markers), (
        "queued work starts after a token is released"
    )
    for marker in markers:
        cgroup = json.loads(marker.read_text())["cgroup"]
        assert scenario.harness.compilers_slice in cgroup, cgroup
