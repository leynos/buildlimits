"""Reproduce lost error channels and stage-free launch diagnostics.

Disposable grandchildren inject one syscall fault while their parent collects
only the launch error channel. Run from the repository root with ``uv run
pytest -v
tests/test_launch_failure_pressure.py``.
"""

import errno
import os

import pytest
from descriptor_helpers import isolated
from launch_helpers import plan, wait_reaped

from buildlimits import launch


def _failure(report: int, *, collision: bool) -> object:
    """Inject a placement or exec failure and collect the bounded launch result."""
    del report
    error_read, error_write = os.pipe()
    barrier_read, barrier_write = os.pipe()
    sources = [os.open("/dev/null", os.O_RDWR) for _ in range(2)] if collision else []
    targets = (error_write, 63) if collision else ()
    spec = launch.LaunchSpec(
        ("/bin/true", "secret-argument"),
        {"SECRET_TOKEN": "credential-probe"},
        ".",
        tuple(sources),
        targets,
        plan(umask=0o22),
    )
    pid = os.fork()
    if pid == 0:  # pragma: no cover - disposable grandchild
        try:
            patch = pytest.MonkeyPatch()
            _inject_fault(patch, collision)
            launch._become_compiler(spec, barrier_read, error_write)
        finally:
            os._exit(1)
    child = launch.HeldChild(pid, barrier_write, error_read)
    try:
        os.close(error_write)
        os.close(barrier_read)
        for fd in sources:
            os.close(fd)
        child.release()
        status = wait_reaped(child, timeout=10)
        code = os.waitstatus_to_exitcode(status)
        if code != 127:
            message = f"unexpected launch experiment exit: {code}"
            raise RuntimeError(message)
        diagnostic = launch.read_exec_outcome(child)
        return {"code": code, "diagnostic": diagnostic}
    finally:
        if not child.reaped:
            child.abandon()
        child.close()


def test_partial_relocation_reports_through_the_current_error_channel() -> None:
    """A later dup2 failure must remain visible after writer relocation."""
    result = isolated(lambda report: _failure(report, collision=True), soft=128)
    assert isinstance(result, dict), "the experiment reports structured evidence"
    diagnostic = result["diagnostic"]
    assert diagnostic is not None and "stage=relocate" in diagnostic, result
    assert "errno=5" in diagnostic, "the injected failing syscall remains identifiable"


def test_launch_diagnostics_identify_stage_and_exclude_sensitive_text() -> None:
    """The error channel must name exec and exclude environments and arguments."""
    result = isolated(lambda report: _failure(report, collision=False))
    assert isinstance(result, dict), "the experiment reports structured evidence"
    diagnostic = result["diagnostic"]
    assert diagnostic is not None and "stage=exec" in diagnostic, result
    assert "errno=5" in diagnostic, "the syscall errno is bounded numeric context"
    assert (
        "credential-probe" not in diagnostic and "secret-argument" not in diagnostic
    ), "arbitrary error text, environment values and arguments stay private"


def _inject_fault(patch: pytest.MonkeyPatch, collision: bool) -> None:
    """Inject either a post-writer-move placement fault or a sanitized exec fault."""
    original = os.dup2

    def fail_placement(source: int, target: int, *, inheritable: bool = True) -> int:
        """Fail after the original error writer has become a compiler target."""
        if target == 63:
            raise OSError(errno.EIO, "credential-probe")
        return original(source, target, inheritable=inheritable)

    def fail_exec(*_args: object) -> None:
        """Make exec fail without copying its arbitrary error text into diagnostics."""
        raise OSError(errno.EIO, "credential-probe")

    patch.setattr(
        os,
        "dup2" if collision else "execvpe",
        fail_placement if collision else fail_exec,
    )
