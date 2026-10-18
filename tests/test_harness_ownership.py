"""Harness shutdown must not close a descriptor that reuses its old pool slot."""

import contextlib
import os
import subprocess
import typing as typ

import harness_processes
import pytest
import systemd_harness
from descriptor_helpers import isolated, open_fds
from systemd_harness import Harness

if typ.TYPE_CHECKING:
    from pathlib import Path


def _close_twice(report: int) -> object:
    """Close owned harness resources before opening an unrelated replacement."""
    del report
    baseline = open_fds()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            systemd_harness.subprocess,
            "run",
            lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0),
        )
        harness = Harness(1)
        holder = harness._holder
        assert holder is not None, "the harness owns its initial pool handle"
        harness.close()
        replacement = os.open("/dev/null", os.O_RDONLY)
        assert replacement == holder, "the reproduction reuses the released pool slot"
        try:
            harness.close()
            try:
                os.fstat(replacement)
                preserved = True
            except OSError:
                preserved = False
        finally:
            with contextlib.suppress(OSError):
                os.close(replacement)
    return {"preserved": preserved, "baseline": open_fds() == baseline}


def test_harness_shutdown_is_idempotent_across_descriptor_reuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repeated cleanup preserves unrelated handles and restores the baseline."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    result = isolated(_close_twice)
    assert isinstance(result, dict), "the child reports descriptor-reuse evidence"
    assert result["preserved"], (
        "shutdown closes only descriptors still owned by the harness"
    )
    assert result["baseline"], "harness teardown restores its exact descriptor baseline"


@pytest.mark.parametrize(
    ("hard", "expected"), [(65536, 65536), (524288, 524288), (-1, 524288)]
)
def test_pressure_limit_respects_user_manager_ceiling(
    monkeypatch: pytest.MonkeyPatch, hard: int, expected: int
) -> None:
    """A smaller manager ceiling remains compatible with high-target pressure."""
    monkeypatch.setattr(
        harness_processes,
        "run",
        lambda argv: subprocess.CompletedProcess(argv, 0, stdout="123\n"),
    )
    monkeypatch.setattr(
        harness_processes.resource, "prlimit", lambda *_args: (1024, hard)
    )
    assert harness_processes.manager_nofile_limit() == expected, (
        "fixture hard limit must respect the actual user-manager ceiling"
    )
