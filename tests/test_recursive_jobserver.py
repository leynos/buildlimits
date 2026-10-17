"""Recursive adapter scope reuse preserves pipe and FIFO Cargo jobservers."""

import os
import sys
from pathlib import Path

import pytest
from descriptor_helpers import isolated, open_fds
from launch_helpers import outcome, plan

from buildlimits import launch

_COMPILER_SCRIPT = """import os
value = os.environ["CARGO_MAKEFLAGS"].removeprefix("--jobserver-auth=")
if value.startswith("fifo:"):
    owned = os.open(value.removeprefix("fifo:"), os.O_RDWR)
    read = write = owned
else:
    read, write = map(int, value.split(","))
token = os.read(read, 1)
os.write(write, token)
print(token.decode())
"""


def _recursive(report: int, fifo: str | None) -> object:
    """Place caller handles, then invoke a second adapter in the admitted scope."""
    del report
    baseline = open_fds()
    if fifo is None:
        job_read, job_write = os.pipe()
        auth = "126,127"
    else:
        os.mkfifo(fifo)
        job_read = job_write = os.open(fifo, os.O_RDWR | os.O_CLOEXEC)
        auth = "fifo:" + fifo
    os.write(job_write, b"+")
    read, write = os.pipe()
    script = f"""import sys
from pathlib import PurePosixPath
from buildlimits import adapter, cgroups

def unexpected_admission(*_args, **_kwargs):
    raise AssertionError("nested entry point must reuse admission")

adapter.run = unexpected_admission
cgroups.own_path = lambda: PurePosixPath(
    '/user.slice/buildlimits-compilers.slice/buildlimits-test-g1.scope'
)
sys.exit(adapter._main([sys.executable, '-c', {_COMPILER_SCRIPT!r}], cacheable=False))
"""
    sources = (write, job_read, job_write) if fifo is None else (write,)
    targets = (1, 126, 127) if fifo is None else (1,)
    spec = launch.LaunchSpec(
        (sys.executable, "-c", script),
        {"CARGO_MAKEFLAGS": "--jobserver-auth=" + auth},
        ".",
        sources,
        targets,
        plan(umask=0o22),
    )
    child = launch.spawn_held(spec)
    try:
        os.close(write)
        write = -1
        child.release()
        code, message = outcome(child)
        output = os.read(read, 4096).decode()
        os.set_blocking(job_read, False)
        token = os.read(job_read, 1).decode()
        result = {
            "code": code,
            "message": message,
            "output": output,
            "returned_byte": token,
        }
    finally:
        child.abandon()
        child.close()
        os.close(read)
        if write >= 0:
            os.close(write)
        os.close(job_read)
        if job_write != job_read:
            os.close(job_write)
    result["baseline"] = open_fds() == baseline
    return result


@pytest.mark.parametrize("jobserver", ["pipe", "fifo"])
def test_recursive_adapter_keeps_jobserver_contract(
    tmp_path: Path, jobserver: str
) -> None:
    """A nested adapter executes under its existing token and returns Cargo's token."""
    fifo = str(tmp_path / "jobserver") if jobserver == "fifo" else None
    result = isolated(lambda report: _recursive(report, fifo))
    assert isinstance(result, dict), "the child reports its inherited token round trip"
    assert result["code"] == 0 and result["message"] is None, result
    assert result["output"] == "+\n" and result["returned_byte"] == "+", result
    assert result["baseline"], "recursive execution leaves no caller descriptors behind"
