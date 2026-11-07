"""Support the opt-in real-kache acceptance check.

kache has no service for the supervisor to own: an operator runs
``kache daemon run`` and every admitted group reaches it through
``KACHE_SOCKET_PATH``. This module supplies what checking that against the
real binary needs -- the collection marks, a private daemon, and readers for
the two records kache publishes.

The environment is built from nothing rather than filtered from this process,
because kache resolves its store, socket and runtime directory from the
environment: one inherited ``KACHE_*`` variable would point the daemon and a
group at different endpoints. No ``KACHE_RUNTIME_DIR`` is named on either
side, so both resolve the runtime directory to the store they were given and
the event log lands at ``<store>/events.jsonl``.

See ``docs/adr-006-kache-cache-backend.md``.
"""

import json
import os

# ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell.
import subprocess
import time
import typing as typ
from contextlib import contextmanager

import pytest
from host_check_marks import (
    HostCheck,
    decoded_document,
    end_process,
    host_check_marks,
)

from buildlimits.kache import LOCAL_ONLY, SCHEDULER_DISABLE

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

#: The kache releases this check has been verified against. ADR 006 records
#: why support is a closed set rather than a range: kache's cache-key recipes
#: and store layout may change within a major series, and it releases weekly.
SUPPORTED_KACHE_VERSIONS = frozenset({"1.2.2", "2.0.0-rc.1"})

_LINUX_REASON = "the kache host check is only supported on Linux"
_VERSION_ERROR_REASON = "could not verify the kache version"
_UNSUPPORTED_VERSION_REASON = "the host check requires a supported kache version"
_OPT_IN_REASON = "set BUILD_LIMITS_KACHE_HOST_CHECK=1 to run the host check"

#: ``daemon status`` and ``stats --full`` answer immediately against a healthy
#: daemon; these bound a wedged one so teardown cannot hang the session.
_PROBE_TIMEOUT_SECONDS = 5.0
_READY_TIMEOUT_SECONDS = 20.0
_STOP_TIMEOUT_SECONDS = 5.0


def kache_host_marks() -> list[pytest.MarkDecorator]:
    """Mark a Linux-only opt-in test and skip hosts without a supported kache.

    Example
    -------
    Use ``pytestmark = kache_host_marks()`` in a host-check module.

    Returns
    -------
    list[pytest.MarkDecorator]
        The opt-in and version-availability marks for the host check.
    """
    check = HostCheck(
        marker=pytest.mark.kache_host_check,
        opt_in_variable="BUILD_LIMITS_KACHE_HOST_CHECK",
        binary="kache",
        versions=SUPPORTED_KACHE_VERSIONS,
        unavailable_reason=_UNSUPPORTED_VERSION_REASON,
        version_error_reason=_VERSION_ERROR_REASON,
    )
    return host_check_marks(
        check,
        platform_reason=_LINUX_REASON,
        opt_in_reason=_OPT_IN_REASON,
    )


def _base_environment(tmp_path: Path, toolchain_bin: Path) -> dict[str, str]:
    """Build the private environment shared by the daemon and every group.

    The daemon is given the same policy the supervisor imposes on admitted
    groups, so the check cannot pass under a scheduler the backend disables.
    No ``KACHE_RUNTIME_DIR`` is named, which is deliberate: both sides then
    resolve the runtime directory to the store they were given.

    Parameters
    ----------
    tmp_path : Path
        The test's private directory.
    toolchain_bin : Path
        The directory holding the scripted compiler, put first on ``PATH``.

    Returns
    -------
    dict[str, str]
        The isolated kache and fake-compiler environment.
    """
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "cache").mkdir()
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": os.pathsep.join((str(toolchain_bin), "/usr/bin:/bin")),
        "KACHE_CACHE_DIR": str(tmp_path / "cache"),
        "KACHE_SOCKET_PATH": str(tmp_path / "daemon.sock"),
        "KACHE_LOCAL_ONLY": LOCAL_ONLY,
        "KACHE_SCHEDULER": SCHEDULER_DISABLE,
    }


@contextmanager
def _private_daemon(
    executable: str,
    environment: dict[str, str],
    log_path: Path,
) -> cabc.Generator[subprocess.Popen[bytes]]:
    """Run a private foreground daemon and stop it even when a test fails.

    ``kache daemon run`` keeps the daemon a child of this process rather than
    a service outside it, and its own session keeps a stop signal from
    reaching this process's group. Its output goes to a log a failure names.

    Example
    -------
    ``with _private_daemon(kache, env, log) as daemon: ...``

    Yields
    ------
    subprocess.Popen[bytes]
        The daemon process, whose ``returncode`` is set once it has stopped.
    """
    with log_path.open("wb") as log_file:
        # ruff: ignore[subprocess-without-shell-equals-true] - daemon argv.
        process = subprocess.Popen(
            [executable, "daemon", "run"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            _await_ready(executable, environment, process, log_path)
            yield process
        finally:
            _stop_daemon(executable, environment, process)


def _await_ready(
    executable: str,
    environment: dict[str, str],
    process: subprocess.Popen[bytes],
    log_path: Path,
) -> None:
    """Block until the private daemon answers for its own socket."""
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"kache daemon exited early; see {log_path}")
        if _reports_running(executable, environment):
            return
        time.sleep(0.05)
    pytest.fail(f"kache daemon did not become ready; see {log_path}")


def _reports_running(executable: str, environment: cabc.Mapping[str, str]) -> bool:
    """Report whether the status probe names a running daemon.

    ``kache daemon status --json`` exits zero whether or not a daemon runs, so
    the answer is the body. Every way the probe can fail reads as "not yet",
    because the caller is polling and its deadline decides when to stop.

    Returns
    -------
    bool
        ``True`` only when the document reports ``daemon_running: true``.
    """
    try:
        # ruff: ignore[subprocess-without-shell-equals-true] - probe argv.
        completed = subprocess.run(
            [executable, "daemon", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            env=dict(environment),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    if completed.returncode != 0:
        return False
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return False
    return isinstance(document, dict) and document.get("daemon_running") is True


def _stop_daemon(
    executable: str,
    environment: cabc.Mapping[str, str],
    process: subprocess.Popen[bytes],
) -> None:
    """Stop the private daemon, bounding every wait for it to exit.

    ``end_process`` runs even when the stop command outlives its own timeout:
    ``subprocess.run`` raises ``TimeoutExpired`` regardless of
    ``check=False``, and the daemon was started in its own session, so a
    teardown that skipped the kill would leave it holding the private store
    and socket after the test had finished.
    """
    try:
        # ruff: ignore[subprocess-without-shell-equals-true] - stop argv, no shell.
        subprocess.run(
            [executable, "daemon", "stop"],
            check=False,
            capture_output=True,
            env=dict(environment),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    finally:
        end_process(process, timeout=_STOP_TIMEOUT_SECONDS)


def _read_events(store: Path) -> list[dict[str, object]]:
    """Read the per-crate record kache appends for every request.

    This is what makes a cache hit observable rather than inferred from
    timing: a row for a served compile carries ``compiler_runs: 0``, so the
    replay provably did not run the compiler. A missing log reads as no
    events, which is what a daemon that has served nothing legitimately has.

    Parameters
    ----------
    store : Path
        The kache store, beside which the runtime directory resolves.

    Returns
    -------
    list[dict[str, object]]
        One decoded record per logged request, in service order.

    Raises
    ------
    TypeError
        If a line is not a JSON object.
    """
    path = store / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            message = "kache wrote a non-object event"
            raise TypeError(message)
        events.append(typ.cast("dict[str, object]", value))
    return events


def _read_summary(
    executable: str, environment: cabc.Mapping[str, str]
) -> dict[str, object]:
    """Read kache's aggregate counters for the store.

    ``--full`` keeps the read daemon-free. Plain ``kache stats --json`` prints
    counters too, but with no daemon answering it starts one to collect them,
    which would change the deployment under test and leave a process behind
    in it.

    Parameters
    ----------
    executable : str
        The kache client to run.
    environment : cabc.Mapping[str, str]
        The private environment, naming the store.

    Returns
    -------
    dict[str, object]
        The document's ``summary`` counters.

    Raises
    ------
    ValueError
        If the document is not kache's own supported ``stats`` envelope.
    TypeError
        If the JSON value or its ``summary`` member is not an object.
    """
    # ruff: ignore[subprocess-without-shell-equals-true] - supplied argv.
    completed = subprocess.run(
        [executable, "stats", "--json", "--full"],
        check=True,
        capture_output=True,
        text=True,
        env=dict(environment),
        timeout=_PROBE_TIMEOUT_SECONDS,
    )
    document = decoded_document(completed.stdout, source="kache")
    envelope = (
        document.get("schema_version"),
        document.get("command"),
        document.get("success"),
    )
    if envelope != (1, "stats", True):
        message = "kache returned an unsupported statistics document"
        raise ValueError(message)
    summary = document.get("summary")
    if not isinstance(summary, dict):
        message = "the statistics document carries no summary object"
        raise TypeError(message)
    return typ.cast("dict[str, object]", summary)
