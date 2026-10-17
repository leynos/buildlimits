"""Run CrossHair over the symbolic contracts in ``crosshair_contracts``."""

import subprocess
import sys
from pathlib import Path

CONTRACTS = Path(__file__).with_name("crosshair_contracts.py")


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
    )
    assert result.returncode == 0, result.stdout + result.stderr
