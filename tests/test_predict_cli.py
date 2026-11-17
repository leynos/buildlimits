"""End-to-end contracts for the benchmark command."""

import typing as typ
from pathlib import Path

import pytest
from predict_test_helpers import (
    benchmark_argv,
    read_results,
    write_admitted_snapshot,
    write_fast_config,
    write_snapshot,
)

from buildlimits.predict import benchmark_cli
from buildlimits.predict.config_loader import CONFIG_PATH


def _run_admitted_benchmark(tmp_path: Path, output: Path, count: int) -> int:
    """Run one benchmark over a synthetic snapshot with admission times.

    Returns
    -------
    int
        The command's exit status.
    """
    snapshot = write_admitted_snapshot(tmp_path, count)
    return benchmark_cli.main(
        benchmark_argv(snapshot, output, write_fast_config(tmp_path))
    )


def test_benchmark_command_writes_both_result_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One run writes results.json, results.md and the timing appendix."""
    output = tmp_path / "out"

    status = _run_admitted_benchmark(tmp_path, output, 400)

    printed = capsys.readouterr().out
    document, markdown = read_results(output)
    assert status == 0, "the benchmark completes"
    assert document["status"] == "ok", "enough labels for a verdict"
    assert len(document["predictors"]) == 9, "six singles and three ensembles"
    assert "Held-out comparison" in markdown, "the comparison table is printed"
    assert "Held-out comparison" in printed, "the command prints the table"
    assert (output / "timings.json").exists(), "latency is reported separately"
    assert document["latency"]["file"] == "timings.json", "the pointer is explicit"


def test_a_store_snapshot_without_admission_times_predicts_nothing(
    tmp_path: Path,
) -> None:
    """The live store records no admission time, so no event is predicted.

    The honest answer is the count of what is missing, not a prediction made
    from a label that had already arrived.
    """
    output = tmp_path / "out"
    snapshot = write_snapshot(tmp_path, 400)

    status = benchmark_cli.main(
        benchmark_argv(snapshot, output, write_fast_config(tmp_path))
    )

    document, markdown = read_results(output)
    assert status == 0, "the run still produces both files"
    assert document["status"] == "insufficient admission times", (
        "the missing admission times set the status"
    )
    assert document["data"]["stats"]["admission_usable"] == 0, (
        "no row offers a decision time"
    )
    assert document["replay"]["unavailable_admission"] == 400, (
        "every event is counted as unpredictable"
    )
    assert "Insufficient admission times" in markdown, "the report explains why"


def test_benchmark_defaults_report_insufficient_data_honestly(
    tmp_path: Path,
) -> None:
    """Too little history is stated with counts, never padded or faked."""
    output = tmp_path / "out"
    snapshot = write_snapshot(tmp_path, 30)

    status = benchmark_cli.main(benchmark_argv(snapshot, output))

    document, markdown = read_results(output)
    assert status == 0, "an honest negative result is not a failure"
    assert document["status"] == "insufficient data", "status is explicit"
    assert "2000 required" in document["verdict"], "the threshold is reported"
    assert "Insufficient data" in markdown, "the report says so in prose"
    assert document["data"]["stats"]["labelled"] == 25, "counts are real"


def test_two_runs_produce_identical_result_files(tmp_path: Path) -> None:
    """The same snapshot and config give byte-identical results.json."""
    config = write_fast_config(tmp_path)
    snapshot = write_admitted_snapshot(tmp_path, 400)

    first = tmp_path / "first"
    second = tmp_path / "second"
    for output in (first, second):
        benchmark_cli.main(benchmark_argv(snapshot, output, config))

    assert (first / "results.json").read_bytes() == (
        second / "results.json"
    ).read_bytes(), "deterministic results"
    assert (first / "results.md").read_bytes() == (
        second / "results.md"
    ).read_bytes(), "deterministic report"


@pytest.mark.parametrize("alias", [False, True], ids=["same-path", "symlink"])
def test_benchmark_refuses_an_output_that_would_replace_the_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *, alias: bool
) -> None:
    """A report directory must not destroy the history it was read from."""
    output = tmp_path / "out"
    output.mkdir()
    snapshot = output / "results.json"
    snapshot.write_bytes(write_snapshot(tmp_path, 20).read_bytes())
    if alias:
        link = tmp_path / "alias.jsonl"
        link.symlink_to(snapshot)
        snapshot = link
    before = snapshot.read_bytes()

    status = benchmark_cli.main(
        benchmark_argv(snapshot, output, write_fast_config(tmp_path))
    )

    assert status == 2, "a destructive output path is a usage failure"
    assert "must not overwrite the snapshot" in capsys.readouterr().err, (
        "the refusal names the reason"
    )
    assert snapshot.read_bytes() == before, "the snapshot is untouched"


def test_benchmark_refuses_an_output_that_would_replace_the_packaged_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default parameters are an input even when no option names them.

    A report file linked to the packaged ``config.json`` would otherwise
    replace it, and every later run that takes the defaults would fail to
    load its parameters.
    """
    output = tmp_path / "out"
    output.mkdir()
    (output / "results.md").symlink_to(CONFIG_PATH)
    before = CONFIG_PATH.read_bytes()
    snapshot = write_snapshot(tmp_path, 20)

    status = benchmark_cli.main([
        "--snapshot",
        str(snapshot),
        "--output",
        str(output),
    ])

    assert status == 2, "a destructive output path is a usage failure"
    assert "must not overwrite the config being read" in capsys.readouterr().err, (
        "the refusal names the defaulted input"
    )
    assert CONFIG_PATH.read_bytes() == before, "the packaged parameters are intact"


def test_benchmark_refuses_report_files_that_alias_each_other(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One artefact must not be reached through two names in the report directory.

    A symbolic link between two of the three destinations would let the second
    write replace the first, so the run stops before it writes anything.
    """
    output = tmp_path / "out"
    output.mkdir()
    (output / "results.json").symlink_to(output / "timings.json")

    status = _run_admitted_benchmark(tmp_path, output, 40)

    assert status == 2, "aliased destinations are a usage failure"
    assert "same file" in capsys.readouterr().err, "the refusal names the reason"
    assert not (output / "timings.json").exists(), "nothing was written"


def test_benchmark_reports_an_unwritable_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A report directory that cannot be created is a status, not a traceback."""
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the report directory should be", encoding="utf-8")

    status = _run_admitted_benchmark(tmp_path, blocked, 40)

    assert status == 1, "an unwritable report is a command failure"
    assert "build-limits-predict-benchmark:" in capsys.readouterr().err, (
        "the reason is reported"
    )


def test_benchmark_reports_a_missing_snapshot(tmp_path: Path) -> None:
    """A run without a snapshot fails clearly instead of inventing history."""
    status = benchmark_cli.main(
        benchmark_argv(tmp_path / "absent.jsonl", tmp_path / "out")
    )

    assert status == 1, "missing snapshot is fatal"


def test_benchmark_reports_an_invalid_config(tmp_path: Path) -> None:
    """An unusable parameter file is a usage failure."""
    snapshot = tmp_path / "history.jsonl"
    snapshot.write_text("{}", encoding="utf-8")
    bad = tmp_path / "config.json"
    bad.write_text('{"config_version": 1}\n', encoding="utf-8")

    status = benchmark_cli.main(benchmark_argv(snapshot, tmp_path / "out", bad))

    assert status == 2, "invalid config is a usage failure"


def test_benchmark_rejects_an_unknown_option() -> None:
    """Argparse exits, and the command reports it as a usage failure."""
    assert benchmark_cli.main(["--no-such-option"]) == 2, "usage failure"


def test_the_report_records_the_commit_the_caller_supplies(tmp_path: Path) -> None:
    """The provenance edge is injectable, so a run need not sit in a checkout."""
    output = tmp_path / "out"
    snapshot = write_admitted_snapshot(tmp_path, 400)

    status = benchmark_cli.main(
        benchmark_argv(snapshot, output, write_fast_config(tmp_path)),
        git_sha=lambda: "c" * 40,
    )

    document, markdown = read_results(output)
    assert status == 0, "the benchmark completes"
    assert document["git_sha"] == "c" * 40, "the supplied commit is recorded"
    assert f"- Commit: `{'c' * 40}`" in markdown, "and rendered in the header"


@pytest.fixture(scope="module")
def published_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[int, dict[str, typ.Any], str]:
    """Run one benchmark and share its report with the read-only checks.

    The two checks below inspect the same published numbers, so paying for the
    walk twice would add run time without adding a contract.
    """
    directory = tmp_path_factory.mktemp("published")
    output = directory / "out"
    status = _run_admitted_benchmark(directory, output, 400)
    document, markdown = read_results(output)
    return status, document, markdown


def test_the_published_numbers_agree_with_each_other(
    published_run: tuple[int, dict[str, typ.Any], str],
) -> None:
    """Every interval brackets its difference and every bin counts a scored row."""
    status, document, markdown = published_run

    assert status == 0, "the benchmark completes"
    positions = document["data"]["stats"]["events"]
    for name, entry in document["predictors"].items():
        heldout = entry["held-out"]
        bins = heldout["calibration"]
        assert sum(bin_["count"] for bin_ in bins) == heldout["scored"], (
            f"{name} counts every answered row in exactly one bin"
        )
        interval = entry["against_base_rate"]
        if interval is None:
            continue
        assert interval["low"] <= interval["difference"] <= interval["high"], (
            f"{name} brackets its observed difference"
        )
        assert interval["rows"] <= heldout["scored"], (
            f"{name} pairs no more rows than it answered"
        )
        assert interval["positions"] <= positions, (
            f"{name} spans no more positions than the stream holds"
        )
    assert "| `base-rate` |" in markdown, "the comparison row is rendered"


def test_the_rendered_row_repeats_the_published_base_rate(
    published_run: tuple[int, dict[str, typ.Any], str],
) -> None:
    """The Markdown table and results.json cannot disagree about the leader."""
    status, document, markdown = published_run
    heldout = document["predictors"]["base-rate"]["held-out"]
    row = next(
        line for line in markdown.splitlines() if line.startswith("| `base-rate` |")
    )
    assert status == 0, "the benchmark completes"
    assert f"| {heldout['brier']:.4f} " in row, "the Brier is the published value"
    assert f"| {heldout['coverage']:.1%} " in row, "the coverage is the published share"
    assert f"| {heldout['scored']} " in row, "the answered-row count matches"
