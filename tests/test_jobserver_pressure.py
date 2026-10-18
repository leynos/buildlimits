"""Jobserver pipe inheritance under launch descriptor pressure.

The compiler acquires and returns the caller's token through unchanged descriptor
numbers. Run with ``uv run pytest -v
tests/test_jobserver_pressure.py`` from the repository root.
"""

import os
import sys

from launch_helpers import clean_isolated_launch, outcome, plan

from buildlimits import launch


def _pipe_jobserver(report: int) -> object:
    """Acquire and return one token through the original jobserver numbers."""
    del report
    job_read, job_write = os.pipe()
    os.write(job_write, b"+")
    read, write = os.pipe()
    script = (
        "import os; "
        'auth=os.environ["CARGO_MAKEFLAGS"].split("=")[1]; '
        'r,w=map(int,auth.split(",")); '
        "token=os.read(r,1); os.write(w,token); print(token.decode())"
    )
    spec = launch.LaunchSpec(
        (sys.executable, "-c", script),
        {"CARGO_MAKEFLAGS": "--jobserver-auth=40,41"},
        ".",
        (write, job_read, job_write),
        (1, 40, 41),
        plan(),
    )
    child = launch.spawn_held(spec)
    os.close(write)
    child.release()
    code, message = outcome(child)
    output = os.read(read, 4096).decode()
    os.close(read)
    os.set_blocking(job_read, False)
    returned = os.read(job_read, 1).decode()
    os.close(job_read)
    os.close(job_write)
    return {"code": code, "message": message, "output": output, "token": returned}


def test_pipe_jobserver_numbers_and_token_round_trip_survive_pressure() -> None:
    """The unchanged Cargo jobserver environment names usable inherited pipes."""
    result = clean_isolated_launch(_pipe_jobserver)
    assert result["output"] == "+\n", result
    assert result["token"] == str(result["output"]).strip(), (
        "the caller token is returned exactly once"
    )
