"""The build-limits-predict-benchmark command.

It reads the recorded-history snapshot, walks it forward in time, scores every
predictor and writes the comparison, calibration and ablation tables.
"""

import argparse
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed git command, never a shell.
import sys
from pathlib import Path

from buildlimits.predict.config import load_config
from buildlimits.predict.evaluation import evaluate
from buildlimits.predict.events import fingerprint
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.render import render_timings
from buildlimits.predict.report import Provenance, build_report
from buildlimits.predict.snapshot_store import load_snapshot

DEFAULT_SNAPSHOT = Path("build/predict/history.jsonl")
DEFAULT_OUTPUT = Path("build/predict")


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark end to end and print the comparison tables.

    Returns
    -------
    int
        0 when a report was produced, 2 on invalid usage, 1 when the
        snapshot or config cannot be read.
    """
    parser = argparse.ArgumentParser(prog="build-limits-predict-benchmark")
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT,
        help=f"redacted history to read (default {DEFAULT_SNAPSHOT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"directory for results.json and results.md (default {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="pinned parameters to use"
    )
    try:
        options = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    try:
        config = load_config(options.config)
    except (TypeError, ValueError, OSError) as error:
        print(f"build-limits-predict-benchmark: {error}", file=sys.stderr)
        return 2
    try:
        snapshot = load_snapshot(options.snapshot)
    except (TypeError, ValueError, OSError) as error:
        print(f"build-limits-predict-benchmark: {error}", file=sys.stderr)
        return 1
    walk = walk_forward(snapshot.events, config)
    report = build_report(
        evaluate(walk, config),
        Provenance(
            snapshot=snapshot,
            config=config,
            git_sha=_git_sha(),
            fingerprint=fingerprint(snapshot.events),
        ),
    )
    report.write(options.output)
    print(report.markdown)
    print(render_timings(report.timings))
    written = ", ".join(
        str(options.output / name)
        for name in ("results.json", "results.md", "timings.json")
    )
    print(f"wrote {written}", file=sys.stderr)
    return 0


def _git_sha() -> str:
    """Return the current commit, or an explicit marker when unavailable.

    Returns
    -------
    str
        The full commit hash, or ``unknown``.
    """
    executable = shutil.which("git")
    if executable is None:
        return "unknown"
    try:
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed git argument vector.
            [executable, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return "unknown"
    return result.stdout.strip() or "unknown"
