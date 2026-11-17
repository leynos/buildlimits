"""The build-limits-predict-benchmark command.

It reads the recorded-history snapshot, walks it forward in time, scores every
predictor and writes the comparison, calibration and ablation tables.
"""

import argparse
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed git command, never a shell.
import sys
import typing as typ
from pathlib import Path

from buildlimits.predict.config import load_config
from buildlimits.predict.evaluation import evaluate
from buildlimits.predict.events import fingerprint
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.latency import render_timings
from buildlimits.predict.paths import same_file
from buildlimits.predict.report import RESULT_FILES, Provenance, build_report
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
        ("config", options.config),
    ):
        if source is None:
            continue
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
            git_sha=(git_sha or _git_sha)(),
            fingerprint=fingerprint(snapshot.events),
        ),
    )
    try:
        report.write(options.output)
    except OSError as error:
        print(f"build-limits-predict-benchmark: {error}", file=sys.stderr)
        return 1
    print(report.markdown)
    print(render_timings(report.timings))
    written = ", ".join(str(options.output / name) for name in RESULT_FILES)
    print(f"wrote {written}", file=sys.stderr)
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


def _git_sha() -> str:
    """Return the commit that holds this benchmark, or an explicit marker.

    The commit is read from the repository holding this module rather than
    from the working directory, so a report produced inside an unrelated
    checkout still names the code that produced it. A copy installed inside
    another project's virtual environment sits in that project's repository,
    whose commit is not this benchmark's provenance, so the module's own path
    inside the repository is verified, and Git is asked whether it tracks that
    path, before the commit is accepted.

    Returns
    -------
    str
        The full commit hash, or ``unknown``.
    """
    executable = shutil.which("git")
    if executable is None:
        return "unknown"
    source = Path(__file__).resolve()
    try:
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed git argument vector.
            [
                executable,
                "-C",
                str(source.parent),
                "rev-parse",
                "HEAD",
                "--show-toplevel",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    commit, _, root = result.stdout.partition("\n")
    return commit.strip() if _owns(executable, root.strip(), source) else "unknown"


def _owns(executable: str, root: str, source: Path) -> bool:
    """Return whether the repository at ``root`` holds ``source`` as its module.

    The path comparison rules out a copy that sits anywhere else in a host
    project, and Git is then asked whether it tracks the file: a copy dropped
    at the canonical path inside an unrelated repository would otherwise
    borrow that repository's commit.

    Returns
    -------
    bool
        Whether the module is tracked at its canonical path in that repository.
    """
    canonical = Path(root) / "buildlimits" / "predict" / source.name
    if not root or canonical.resolve() != source:
        return False
    return _tracked(executable, root, source)


def _tracked(executable: str, root: str, source: Path) -> bool:
    """Return whether ``git ls-files`` lists ``source`` in that repository.

    Returns
    -------
    bool
        Whether Git reports the file as tracked.
    """
    try:
        relative = source.relative_to(Path(root).resolve())
    except ValueError:
        return False
    try:
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed git argument vector.
            [
                executable,
                "-C",
                root,
                "ls-files",
                "--error-unmatch",
                "--",
                str(relative),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return result.returncode == 0
