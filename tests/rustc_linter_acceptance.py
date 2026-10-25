"""Run host linter acceptance through held-pool resolution milestones."""

import contextlib
import dataclasses as dc
import json
import typing as typ

from rustc_query_host_checks import (
    QueueWait,
    _assert_driver_probes,
    _finish_barriers,
    _finish_or_fail,
    _is_compile,
    _is_query,
    _package_cache_waits,
    _probe_summary,
    _record_observation,
    _require,
    _result_evidence,
    _wait_for_queued_compile,
)
from rustc_query_host_support import (
    ObservedCommand,
    ObservedProcess,
    ProbeRecord,
    cargo_environment,
    dependency_free_crate,
    hold_pool,
    start_observed,
    timing_entries,
)
from rustc_query_timing_shim import install_timing_shim
from systemd_harness import Harness

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


@dc.dataclass(frozen=True, slots=True)
class LinterAcceptance:
    """Configuration and evidence recorder for one real linter run."""

    cargo: Path
    name: str
    driver_name: str
    command_prefix: list[str]
    command_suffix: list[str]
    tool_versions: dict[str, str]
    extra_environment: cabc.Mapping[str, str] | None
    toolchain: str | None
    queue_timeout: float
    completion_timeout: float
    record_property: cabc.Callable[[str, object], None]


@dc.dataclass(slots=True)
class LinterRun:
    """Live linter command and resources retained through barrier release."""

    harness: Harness
    timing_log: Path
    environment: dict[str, str]
    release: Path
    barriers: list[typ.Any]
    observed: ObservedProcess
    probes: list[ProbeRecord]
    queue_status: dict[str, typ.Any]


def run_linter_acceptance(scenario: LinterAcceptance) -> None:
    """Run one linter to its queued compile and prove metadata can proceed.

    Parameters
    ----------
    scenario : LinterAcceptance
        Linter command, deadlines, and recorded host evidence.
    """
    with contextlib.ExitStack() as owned:
        harness = Harness(1)
        owned.callback(harness.close)
        harness.start()
        linter_run = _start_linter(scenario, owned, harness)
        _record_queue_evidence(scenario, linter_run)
        _prove_metadata_progress(scenario, linter_run, owned)
        _complete_linter(scenario, linter_run)


def _start_linter(
    scenario: LinterAcceptance, owned: contextlib.ExitStack, harness: Harness
) -> LinterRun:
    """Start a linter and wait for its first real compile to queue.

    Returns
    -------
    LinterRun
        Live linter and held-pool state required for later milestones.
    """
    shim, timing_log = install_timing_shim(harness)
    manifest = dependency_free_crate(
        harness.work / scenario.name, f"{scenario.name}_probe"
    )
    _write_toolchain(manifest, scenario.toolchain)
    environment = cargo_environment(
        harness, shim, timing_log, extra=scenario.extra_environment
    )
    release, barriers = hold_pool(owned, harness)
    observed = start_observed(
        owned,
        ObservedCommand(
            argv=_linter_argv(scenario, manifest),
            cwd=manifest.parent,
            environment=environment,
            stdout_path=harness.work / f"{scenario.name}.out",
        ),
    )
    queue_status, probes = _wait_for_queued_compile(
        harness,
        QueueWait(
            observed=observed,
            timing_log=timing_log,
            driver_name=scenario.driver_name,
            timeout=scenario.queue_timeout,
        ),
    )
    _assert_driver_probes(probes, scenario.driver_name)
    _require(condition=queue_status["held"] == harness.slots, message=str(queue_status))
    _require(condition=len(queue_status["queued"]) == 1, message=str(queue_status))
    _require(condition=all(_is_query(entry) for entry in probes), message=str(probes))
    _require(
        condition=not any(_is_compile(entry) for entry in probes), message=str(probes)
    )
    return LinterRun(
        harness,
        timing_log,
        environment,
        release,
        barriers,
        observed,
        probes,
        queue_status,
    )


def _record_queue_evidence(scenario: LinterAcceptance, run: LinterRun) -> None:
    """Record the tool versions and completed queries before the compile waits."""
    for version_name, version in scenario.tool_versions.items():
        scenario.record_property(f"{scenario.name}_{version_name}", version)
    scenario.record_property(
        f"{scenario.name}_queued_probes",
        json.dumps(_probe_summary(run.probes), sort_keys=True),
    )


def _prove_metadata_progress(
    scenario: LinterAcceptance,
    run: LinterRun,
    owned: contextlib.ExitStack,
) -> None:
    """Run metadata with the same Cargo home while the linter compile waits."""
    manifest = dependency_free_crate(
        run.harness.work / f"{scenario.name}-second-metadata",
        f"{scenario.name}_metadata_probe",
    )
    _write_toolchain(manifest, scenario.toolchain)
    environment = dict(run.environment)
    environment["CARGO_TARGET_DIR"] = str(
        run.harness.work / f"{scenario.name}-second-target"
    )
    metadata = start_observed(
        owned,
        ObservedCommand(
            argv=[
                str(scenario.cargo),
                "metadata",
                "--offline",
                "--format-version",
                "1",
                "--manifest-path",
                str(manifest),
            ],
            cwd=manifest.parent,
            environment=environment,
            stdout_path=run.harness.work / f"{scenario.name}-second-metadata.json",
        ),
    )
    metadata_result = _finish_or_fail(metadata, 90, run.harness, run.timing_log)
    _require(
        condition=metadata_result.status == 0,
        message=_result_evidence(metadata_result, timing_entries(run.timing_log)),
    )
    current_status = run.harness.status()
    _require(
        condition=current_status["held"] == run.harness.slots,
        message=str(current_status),
    )
    _require(condition=len(current_status["queued"]) == 1, message=str(current_status))
    entries = timing_entries(run.timing_log)
    _require(
        condition=not any(_is_compile(entry) for entry in entries),
        message=str(entries),
    )
    scenario.record_property(
        f"{scenario.name}_second_metadata_package_cache_waits",
        json.dumps(_package_cache_waits(metadata_result), sort_keys=True),
    )
    _record_observation(
        scenario.record_property,
        f"{scenario.name}_second_metadata",
        metadata_result,
        entries,
    )


def _complete_linter(scenario: LinterAcceptance, run: LinterRun) -> None:
    """Release all held tokens and require the linter compile to finish."""
    run.release.touch()
    linter_result = _finish_or_fail(
        run.observed, scenario.completion_timeout, run.harness, run.timing_log
    )
    _require(
        condition=linter_result.status == 0,
        message=_result_evidence(linter_result, timing_entries(run.timing_log)),
    )
    _finish_barriers(run.barriers)
    final_status = run.harness.status()
    _require(condition=final_status["held"] == 0, message=str(final_status))
    _require(condition=not final_status["queued"], message=str(final_status))
    completed = timing_entries(run.timing_log)
    _require(
        condition=any(
            _is_compile(entry) and entry["status"] == 0 for entry in completed
        ),
        message=str(completed),
    )
    scenario.record_property(
        f"{scenario.name}_completed_probe_durations",
        json.dumps(_probe_summary(completed), sort_keys=True),
    )
    _record_observation(
        scenario.record_property, f"cargo_{scenario.name}", linter_result, completed
    )


def _linter_argv(scenario: LinterAcceptance, manifest: Path) -> list[str]:
    """Build the linter command with the private crate manifest.

    Returns
    -------
    list[str]
        Complete argv for the linter invocation.
    """
    return [
        str(scenario.cargo),
        *scenario.command_prefix,
        "--manifest-path",
        str(manifest),
        *scenario.command_suffix,
    ]


def _write_toolchain(manifest: Path, toolchain: str | None) -> None:
    """Pin a fixture crate to a prebuilt driver toolchain when requested."""
    if toolchain is not None:
        (manifest.parent / "rust-toolchain.toml").write_text(
            f'channel = "{toolchain}"\n'
        )
