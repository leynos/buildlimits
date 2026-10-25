"""Helpers for host acceptance of rustc query admission behaviour."""

import dataclasses as dc
import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell.
import threading
import time
import typing as typ
from pathlib import Path

import pytest
from harness_processes import own_process
from systemd_harness import Harness, wait_for

from buildlimits import adapter

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import contextlib

CLEAN_COMPILER_ENVIRONMENT = (
    "RUSTC",
    "RUSTC_WRAPPER",
    "RUSTC_WORKSPACE_WRAPPER",
    "RUSTFLAGS",
    "CARGO_ENCODED_RUSTFLAGS",
    "DYLINT_RUSTFLAGS",
)


def first_dylint_library(library_path: str) -> Path | None:
    """Return a prebuilt Dylint shared library from caller search paths.

    Parameters
    ----------
    library_path : str
        Platform-separated directories supplied by the caller.

    Returns
    -------
    Path | None
        The first matching shared library, if one is installed.
    """
    for directory in library_path.split(os.pathsep):
        path = Path(directory)
        if path.is_dir():
            matches = sorted(
                candidate
                for candidate in path.iterdir()
                if candidate.is_file() and "@" in candidate.name
            )
            if matches:
                return matches[0]
    return None


def caller_dylint_library() -> tuple[str, Path]:
    """Require the caller library path and locate its first library.

    Returns
    -------
    tuple[str, Path]
        The original library search path and a prebuilt matching library.
    """
    library_path = os.environ.get("DYLINT_LIBRARY_PATH")
    if not library_path:
        pytest.skip("caller-supplied DYLINT_LIBRARY_PATH is unset")
    library = first_dylint_library(library_path)
    if library is None:
        pytest.skip("DYLINT_LIBRARY_PATH contains no loadable Dylint library")
    return library_path, library


def require_host_command(command: str, reason: str) -> None:
    """Skip host acceptance when a required command is absent from PATH."""
    if shutil.which(command) is None:
        pytest.skip(reason)


class ProbeRecord(typ.TypedDict):
    """One complete JSON-line record from the timing shim."""

    argv: list[str]
    started: float
    ended: float
    status: int
    error: typ.NotRequired[str]


@dc.dataclass(frozen=True, slots=True)
class ObservedCommand:
    """Arguments and streams for a timestamped host process."""

    argv: cabc.Sequence[str]
    cwd: Path
    environment: cabc.Mapping[str, str]
    stdout_path: Path


_BARRIER_COMPILER = r"""import os
import time
from pathlib import Path

Path(os.environ["HOST_BARRIER_MARKER"]).write_text(Path("/proc/self/cgroup").read_text())
release = Path(os.environ["HOST_BARRIER_RELEASE"])
while not release.exists():
    time.sleep(0.01)
"""


@dc.dataclass(slots=True)
class ObservedProcess:
    """A live command with timestamped stderr and captured stdout."""

    process: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_lines: list[tuple[float, str]]
    stderr_reader: threading.Thread
    started: float

    def finish(self, timeout: float) -> ProcessResult:
        """Wait for completion and return output plus monotonic observations.

        Returns
        -------
        ProcessResult
            Exit status, captured output, and monotonic timing.

        Raises
        ------
        RuntimeError
            If the stderr reader remains alive after the process exits.
        """
        status = self.process.wait(timeout=timeout)
        self.stderr_reader.join(timeout=5)
        if self.stderr_reader.is_alive():
            msg = "stderr reader did not reach EOF after process exit"
            raise RuntimeError(msg)
        return ProcessResult(
            status=status,
            stdout=self.stdout_path.read_bytes(),
            stderr_lines=tuple(self.stderr_lines),
            started=self.started,
            ended=time.monotonic(),
        )


@dc.dataclass(frozen=True, slots=True)
class ProcessResult:
    """Captured process status, output and monotonic timing."""

    status: int
    stdout: bytes
    stderr_lines: tuple[tuple[float, str], ...]
    started: float
    ended: float

    @property
    def stderr(self) -> str:
        """Join captured stderr lines in their original order."""
        return "".join(line for _, line in self.stderr_lines)


def cargo_environment(
    harness: Harness,
    shim: Path,
    timing_log: Path,
    *,
    extra: cabc.Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a cold, private Cargo environment using the timing shim.

    Parameters
    ----------
    harness : Harness
        The isolated systemd harness.
    shim : Path
        The installed timing shim.
    timing_log : Path
        The shim's JSON-lines output path.
    extra : Mapping[str, str] | None
        Additional caller-supplied environment values.

    Returns
    -------
    dict[str, str]
        A private Cargo environment with inherited compiler overrides removed.
    """
    environment = harness.env.copy()
    for name in CLEAN_COMPILER_ENVIRONMENT:
        environment.pop(name, None)
    environment.pop("RUSTUP_TOOLCHAIN", None)
    cargo_home = harness.work / "cargo-home"
    cargo_home.mkdir(exist_ok=True)
    environment.update({
        "CARGO_HOME": str(cargo_home),
        "CARGO_TARGET_DIR": str(harness.work / "target"),
        "CARGO_TERM_COLOR": "never",
        "PYTHONPATH": os.pathsep.join(
            filter(
                None,
                (
                    str(Path(adapter.__file__).resolve().parent.parent),
                    environment.get("PYTHONPATH", ""),
                ),
            )
        ),
        "RUSTC_QUERY_TIMING_LOG": str(timing_log),
        "RUSTC_WRAPPER": str(shim),
    })
    if extra is not None:
        environment.update(extra)
    return environment


def dependency_free_crate(directory: Path, name: str) -> Path:
    """Write a small library crate and return its manifest path.

    Returns
    -------
    Path
        The created Cargo manifest.
    """
    source = directory / "src"
    source.mkdir(parents=True)
    manifest = directory / "Cargo.toml"
    manifest.write_text(
        f'[package]\nname = "{name}"\nversion = "0.0.0"\nedition = "2021"\n'
        '\n[lib]\npath = "src/lib.rs"\n'
    )
    (source / "lib.rs").write_text("pub fn value() -> u8 { 1 }\n")
    return manifest


def hold_pool(
    owned: contextlib.ExitStack, harness: Harness
) -> tuple[Path, list[subprocess.Popen[bytes]]]:
    """Occupy every token with an isolated compiler waiting for one release file.

    Returns
    -------
    tuple[Path, list[subprocess.Popen[bytes]]]
        The release file and all barrier processes.

    Raises
    ------
    AssertionError
        If any barrier is outside the dedicated compiler slice.
    RuntimeError
        If the pool and its barrier processes do not become ready.
    """
    release = harness.work / "release-barriers"
    owned.callback(release.unlink, missing_ok=True)
    processes: list[subprocess.Popen[bytes]] = []
    markers: list[Path] = []
    for index in range(harness.slots):
        marker = harness.work / f"barrier-{index}.cgroup"
        markers.append(marker)
        process = harness.invoke(
            (shutil.which("python3") or "python3", "-c", _BARRIER_COMPILER),
            env={
                "HOST_BARRIER_MARKER": str(marker),
                "HOST_BARRIER_RELEASE": str(release),
            },
            piped=True,
        )
        processes.append(own_process(owned, process))
    if not wait_for(
        lambda: (
            harness.status()["held"] == harness.slots
            and all(marker.exists() for marker in markers)
        ),
        timeout=60,
    ):
        raise RuntimeError(harness.journal())
    for marker in markers:
        if harness.compilers_slice not in marker.read_text():
            msg = f"barrier compiler was outside {harness.compilers_slice}: {marker}"
            raise AssertionError(msg)
    return release, processes


def start_observed(
    owned: contextlib.ExitStack, command: ObservedCommand
) -> ObservedProcess:
    """Start a command, timestamp stderr lines, and capture stdout to a file.

    Parameters
    ----------
    owned : ExitStack
        The caller's process and pipe ownership stack.
    command : ObservedCommand
        Arguments, working directory, environment and stdout destination.

    Returns
    -------
    ObservedProcess
        The running command and stderr reader.

    Raises
    ------
    RuntimeError
        If the process starts without the requested stderr pipe.
    """
    stderr_lines: list[tuple[float, str]] = []
    started = time.monotonic()
    with command.stdout_path.open("wb") as stdout_file:
        # The returned process outlives this helper; ExitStack owns its cleanup.
        # pylint: disable=consider-using-with
        process = subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - argv is explicit, no shell.
            list(command.argv),
            cwd=command.cwd,
            env=dict(command.environment),
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    if process.stderr is None:
        msg = "stderr capture requires a pipe"
        raise RuntimeError(msg)
    reader = threading.Thread(
        target=_capture_stderr,
        args=(process.stderr, stderr_lines),
        daemon=True,
    )
    reader.start()
    owned.callback(reader.join, 30)
    own_process(owned, process)
    return ObservedProcess(process, command.stdout_path, stderr_lines, reader, started)


def timing_entries(path: Path) -> list[ProbeRecord]:
    """Read completed JSON-lines records from the timing shim.

    Returns
    -------
    list[ProbeRecord]
        Timing records written by completed shim invocations.
    """
    if not path.exists():
        return []
    return [
        typ.cast("ProbeRecord", json.loads(line))
        for line in path.read_text().splitlines()
    ]


def is_version_query(entry: cabc.Mapping[str, object]) -> bool:
    """Recognize the version query shapes used in host probe logs.

    Returns
    -------
    bool
        Whether any argument is a supported version query flag.
    """
    argv = typ.cast("list[str]", entry["argv"])
    return any(argument in {"-vV", "-V", "--version"} for argument in argv)


def is_target_info_query(entry: cabc.Mapping[str, object]) -> bool:
    """Recognize Cargo's stdin target-information query in shim logs.

    Returns
    -------
    bool
        Whether the arguments match Cargo's stdin probe shape.
    """
    argv = typ.cast("list[str]", entry["argv"])
    return (
        "-" in argv
        and "--crate-name" in argv
        and "___" in argv
        and "--print=file-names" in argv
    )


def _capture_stderr(
    stream: typ.BinaryIO,
    output: list[tuple[float, str]],
) -> None:
    """Read stderr without back-pressuring the command and timestamp each line."""
    output.extend(
        (time.monotonic(), raw_line.decode(errors="replace")) for raw_line in stream
    )
