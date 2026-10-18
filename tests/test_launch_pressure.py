"""Disposable-process launch contracts under descriptor pressure.

These experiments exercise launch stages, descriptor identity, caller attributes
and jobserver inheritance without changing the parent runner's limits. Run with
``uv run pytest -v
tests/test_launch_pressure.py`` from the repository root.
"""

import contextlib
import errno
import json
import os
import resource
import signal
import sys
import typing as typ
from pathlib import Path

import pytest
from descriptor_helpers import isolated
from launch_helpers import clean_isolated_launch, outcome, plan

from buildlimits import launch


def _exec_contract(
    report: int, *, target: int, caller_hard: int | None = None
) -> object:
    """Launch with low limits and return the compiler's descriptor/attribute view."""
    del report
    read, write = os.pipe()
    source = os.open("/dev/null", os.O_RDWR)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    # A target above soft must remain open when the caller's limit is restored.
    script = (
        "import os,resource,json; "
        f"os.fstat({target}); "
        'print(json.dumps({"soft":resource.getrlimit(resource.RLIMIT_NOFILE)[0],'
        '"hard":resource.getrlimit(resource.RLIMIT_NOFILE)[1],'
        '"umask":os.umask(0o27), "nice":os.getpriority(os.PRIO_PROCESS,0)}))'
    )
    spec = launch.LaunchSpec(
        (sys.executable, "-c", script),
        {},
        str(Path.cwd()),
        (write, source),
        (1, target),
        plan({"RLIMIT_NOFILE": (soft, hard if caller_hard is None else caller_hard)}),
    )
    child = launch.spawn_held(spec)
    os.close(write)
    os.close(source)
    child.release()
    code, message = outcome(child)
    data = os.read(read, 4096)
    os.close(read)
    return {
        "code": code,
        "message": message,
        "attributes": json.loads(data) if data else None,
    }


@pytest.mark.parametrize("target", [63, 127])
def test_high_targets_exec_with_the_callers_soft_limit(target: int) -> None:
    """Targets near and above soft survive relocation and caller-limit restore."""
    result = isolated(lambda report: _exec_contract(report, target=target))
    assert isinstance(result, dict), "the experiment produces structured evidence"
    assert result["code"] == 0 and result["message"] is None, result
    assert result["attributes"]["soft"] == 64, (
        "the compiler receives the caller soft limit"
    )
    assert result["attributes"]["umask"] == 0o27, (
        "the compiler receives the caller umask"
    )


@pytest.mark.parametrize(
    "stage",
    [
        "close-inherited",
        "barrier",
        "relocation-limit",
        "relocate",
        "signals",
        "cwd",
        "umask",
        "niceness",
        "rlimit:RLIMIT_NOFILE",
        "exec",
    ],
)
def test_setup_stage_failures_are_bounded_and_non_sensitive(stage: str) -> None:
    """Each injected setup failure names its stage without environments or argv."""
    result = isolated(lambda report: _stage_experiment(report, stage))
    assert isinstance(result, dict), "the child emits structured fault evidence"
    message = result["message"]
    assert result["code"] == 127, "every setup failure exits with exec-failed status"
    assert f"stage={stage}" in message and "errno=5" in message, result
    assert len(message.encode("ascii")) < 4096, "the diagnostic fits below PIPE_BUF"
    assert "super-secret" not in message and "secret-argument" not in message, (
        "diagnostics exclude arguments, environments and arbitrary exception strings"
    )
    assert "nofile=" in message and "highest_target=" in message, (
        "bounded numeric context survives pressure"
    )


def _stage_experiment(report: int, stage: str) -> object:
    """Inject one setup fault in a grandchild, retaining an external reporter."""
    del report
    error_read, error_write = os.pipe()
    barrier_read, barrier_write = os.pipe()
    spec = launch.LaunchSpec(
        ("/bin/true", "secret-argument"),
        {"SECRET_TOKEN": "super-secret"},
        ".",
        (),
        (),
        plan({"RLIMIT_NOFILE": resource.getrlimit(resource.RLIMIT_NOFILE)}),
    )
    pid = os.fork()
    if pid == 0:  # pragma: no cover - isolated grandchild
        try:
            targets = {
                "close-inherited": (launch.descriptors, "close_unrelated"),
                "barrier": (os, "read"),
                "relocation-limit": (launch.descriptors, "relocation_plan"),
                "relocate": (launch.descriptors, "execute_plan"),
                "signals": (signal, "pthread_sigmask"),
                "cwd": (os, "chdir"),
                "umask": (os, "umask"),
                "niceness": (os, "setpriority"),
                "rlimit:RLIMIT_NOFILE": (resource, "setrlimit"),
                "exec": (os, "execvpe"),
            }

            def fail(*_args: object, **_kwargs: object) -> typ.NoReturn:
                """Raise an error whose sensitive message must not be forwarded."""
                raise OSError(errno.EIO, "super-secret")

            owner, name = targets[stage]
            pytest.MonkeyPatch().setattr(owner, name, fail)
            launch._become_compiler(spec, barrier_read, error_write)
        finally:
            os._exit(1)
    os.close(error_write)
    os.close(barrier_read)
    with contextlib.suppress(BrokenPipeError):
        os.write(barrier_write, b"go")
    os.close(barrier_write)
    _, status = os.waitpid(pid, 0)
    message = os.read(error_read, 4096).decode("ascii")
    os.close(error_read)
    return {"code": os.waitstatus_to_exitcode(status), "message": message}


def _released_outcome(child: launch.HeldChild, read: int) -> dict[str, object]:
    """Release a held child and collect its outcome and its standard output.

    Returns
    -------
    dict[str, object]
        The exit code, any setup diagnostic and what the compiler printed.
    """
    child.release()
    code, message = outcome(child)
    output = os.read(read, 4096).decode()
    os.close(read)
    return {"code": code, "message": message, "output": output}


def _dense_launch(report: int) -> object:
    """Fill the forked launch child's table after all launch channels exist."""
    del report
    read, write = os.pipe()
    error_read, error_write = os.pipe()
    barrier_read, barrier_write = os.pipe()
    source = os.open("/dev/null", os.O_RDWR)
    spec = launch.LaunchSpec(
        ("/bin/sh", "-c", "echo ready"), {}, ".", (write, source), (1, 63), plan()
    )
    pid = os.fork()
    if pid == 0:  # pragma: no cover - isolated grandchild
        try:
            while True:
                os.dup(source)
        except OSError as error:
            if error.errno != errno.EMFILE:
                os._exit(1)
        try:
            launch._become_compiler(spec, barrier_read, error_write)
        finally:
            os._exit(1)
    os.close(write)
    os.close(error_write)
    os.close(barrier_read)
    os.close(source)
    child = launch.HeldChild(pid, barrier_write, error_read)
    os.set_blocking(error_read, False)
    return _released_outcome(child, read)


def test_dense_inherited_launch_table_recovers_before_exec() -> None:
    """Reclamation must make exec possible at soft64 after child-local EMFILE."""
    result = clean_isolated_launch(_dense_launch)
    assert result["output"] == "ready\n", result


def _high_unrelated(report: int) -> object:
    """Inherit a descriptor above a lowered limit and prove it is reclaimed."""
    del report
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    source = os.open("/dev/null", os.O_RDWR)
    resource.setrlimit(resource.RLIMIT_NOFILE, (129, hard))
    os.dup2(source, 128)
    resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    read, write = os.pipe()
    script = "if [ -e /proc/self/fd/128 ]; then exit 9; fi; echo reclaimed"
    spec = launch.LaunchSpec(("/bin/sh", "-c", script), {}, ".", (write,), (1,), plan())
    child = launch.spawn_held(spec)
    os.close(write)
    os.close(source)
    os.close(128)
    return _released_outcome(child, read)


def test_unrelated_inherited_slots_above_a_lowered_limit_are_closed() -> None:
    """Reclamation covers inherited slots beyond the effective soft ceiling."""
    result = clean_isolated_launch(_high_unrelated)
    assert result["output"] == "reclaimed\n", result


def test_targets_survive_a_lowered_caller_hard_limit() -> None:
    """Placement uses supervisor headroom before the compiler hard limit drops."""
    result = isolated(lambda report: _exec_contract(report, target=127, caller_hard=64))
    assert isinstance(result, dict), "the child reports its final limits"
    assert result["code"] == 0 and result["message"] is None, result
    assert result["attributes"]["soft"] == 64 and result["attributes"]["hard"] == 64, (
        "caller limits are applied after descriptor placement"
    )
