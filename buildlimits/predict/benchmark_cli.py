"""The build-limits-predict-benchmark command.

It reads the recorded-history snapshot, walks it forward in time, scores every
predictor and writes the comparison, calibration and ablation tables.
"""

import argparse
import sys
import typing as typ
from pathlib import Path

from buildlimits.predict.config import CONFIG_PATH, load_config
from buildlimits.predict.evaluation import evaluate
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.latency import render_timings
from buildlimits.predict.paths import same_file
from buildlimits.predict.provenance import git_revision
from buildlimits.predict.report import (
    RESULT_FILES,
    Provenance,
    Report,
    build_report,
)
from buildlimits.predict.snapshot_codec import fingerprint
from buildlimits.predict.snapshot_store import load_snapshot

if typ.TYPE_CHECKING:
    import collections.abc as cabc

DEFAULT_SNAPSHOT = Path("build/predict/history.jsonl")
DEFAULT_OUTPUT_DIR = Path("build/predict")


def main(
    argv: list[str] | None = None,
    *,
    git_sha: cabc.Callable[[], str] | None = None,
) -> int:
    """Run the benchmark end to end and print the comparison tables.

    Parameters
    ----------
    argv : list[str] | None
        The command line, or None to read ``sys.argv``.
    git_sha : Callable[[], str] | None
        The commit the report records. None reads it from this checkout; a test
        supplies its own so it need not sit inside a Git checkout.

    Returns
    -------
    int
        0 when a report was produced, 1 when the snapshot cannot be read or the
        report cannot be written, and 2 on invalid usage or an unreadable
        config.
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
        default=DEFAULT_OUTPUT_DIR,
        help=f"directory for the report files (default {DEFAULT_OUTPUT_DIR})",
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
    for label, source in (
        ("snapshot", options.snapshot),
        # The packaged parameters are an input even when the option is absent,
        # so they are protected exactly as an explicit file would be.
        ("config", CONFIG_PATH if options.config is None else options.config),
    ):
        collision = _input_collision(source, options.output)
        if collision is not None:
            print(
                "build-limits-predict-benchmark: --output must not overwrite the "
                f"{label} being read ({collision}); choose another directory",
                file=sys.stderr,
            )
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
            git_sha=(git_sha or git_revision)(),
            fingerprint=fingerprint(snapshot.events),
        ),
    )
    status = _write_report(report, options.output)
    if status:
        return status
    print(report.markdown)
    print(render_timings(report.timings))
    written = ", ".join(str(options.output / name) for name in RESULT_FILES)
    print(f"wrote {written}", file=sys.stderr)
    return 0


def _write_report(report: Report, output: Path) -> int:
    """Write the report and translate a refusal into a status.

    Returns
    -------
    int
        Zero when the files were written, two when the destinations alias each
        other, and one when the directory cannot be written.
    """
    try:
        report.write(output)
    except ValueError as error:
        print(f"build-limits-predict-benchmark: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"build-limits-predict-benchmark: {error}", file=sys.stderr)
        return 1
    return 0


def _input_collision(source: Path, output: Path) -> Path | None:
    """Return the report file that would replace one of the inputs, if any.

    Every file the report writes is checked before the first one is created.
    A report directory that holds the snapshot would destroy the history the
    next run has to read, and one that holds the config would destroy the
    parameters this run was told to use.

    Returns
    -------
    Path | None
        The colliding destination, or None when all three are distinct files.
    """
    destinations = (output / name for name in RESULT_FILES)
    return next(
        (destination for destination in destinations if same_file(source, destination)),
        None,
    )
