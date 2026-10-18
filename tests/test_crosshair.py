"""Run CrossHair over the symbolic contracts in ``crosshair_contracts``."""

import subprocess
import sys
from pathlib import Path

import pytest

CONTRACTS = Path(__file__).with_name("crosshair_contracts.py")

# The per-condition timeout bounds each contract, not the run: the total grows
# with the number of contracts. The whole run gets its own bound, set below
# the test's so the child is reaped rather than orphaned when it stalls.
RUN_TIMEOUT_SECONDS = 100


@pytest.mark.timeout(RUN_TIMEOUT_SECONDS + 20)
def test_crosshair_finds_no_counterexample() -> None:
    """Symbolic execution finds no input that breaks the limit contracts."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "crosshair",
            "check",
            "--analysis_kind=asserts",
            "--per_condition_timeout=20",
            str(CONTRACTS),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=CONTRACTS.parent,
        timeout=RUN_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stdout + result.stderr
