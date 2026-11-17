"""End-to-end contracts for the benchmark command."""

import json
import typing as typ
from pathlib import Path
from unittest import mock

import pytest
from predict_test_helpers import write_fast_config, write_snapshot

from buildlimits.predict import benchmark_cli

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _results(output: Path) -> tuple[dict[str, typ.Any], str]:
    """Read the two deterministic result files one run wrote.

    Returns
    -------
    tuple[dict[str, object], str]
        The parsed results document and the rendered Markdown.
    """
    document = json.loads((output / "results.json").read_text(encoding="utf-8"))
    markdown = (output / "results.md").read_text(encoding="utf-8")
    return document, markdown


def _git(
    monkeypatch: pytest.MonkeyPatch,
    which: cabc.Callable[[str], str | None],
    run: object = None,
) -> None:
    """Make ``git`` discovery and, when supplied, its invocation answer predictably."""
    monkeypatch.setattr(benchmark_cli.shutil, "which", which)
    if run is not None:
        monkeypatch.setattr(benchmark_cli.subprocess, "run", run)


def test_benchmark_command_writes_both_result_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One run writes results.json, results.md and the timing appendix."""
    output = tmp_path / "out"
    snapshot = write_snapshot(tmp_path, 400)
    capsys.readouterr()

    status = benchmark_cli.main([
        "--snapshot",
        str(snapshot),
        "--output",
        str(output),
        "--config",
        str(write_fast_config(tmp_path)),
    ])

    printed = capsys.readouterr().out
    document, markdown = _results(output)
    assert status == 0, "the benchmark completes"
    assert document["status"] == "ok", "enough labels for a verdict"
    assert len(document["predictors"]) == 9, "six singles and three ensembles"
    assert "Held-out comparison" in markdown, "the comparison table is printed"
    assert "Held-out comparison" in printed, "the command prints the table"
    assert (output / "timings.json").exists(), "latency is reported separately"


def test_benchmark_defaults_report_insufficient_data_honestly(
    tmp_path: Path,
) -> None:
    """Too little history is stated with counts, never padded or faked."""
    output = tmp_path / "out"
    snapshot = write_snapshot(tmp_path, 30)

    status = benchmark_cli.main([
        "--snapshot",
        str(snapshot),
        "--output",
        str(output),
    ])

    document, markdown = _results(output)
    assert status == 0, "an honest negative result is not a failure"
    assert document["status"] == "insufficient data", "status is explicit"
    assert "2000 required" in document["verdict"], "the threshold is reported"
    assert "Insufficient data" in markdown, "the report says so in prose"
    assert document["data"]["stats"]["labelled"] == 25, "counts are real"


def test_two_runs_produce_identical_result_files(tmp_path: Path) -> None:
    """The same snapshot and config give byte-identical results.json."""
    config = write_fast_config(tmp_path)
    snapshot = write_snapshot(tmp_path, 400)

    first = tmp_path / "first"
    second = tmp_path / "second"
    for output in (first, second):
        benchmark_cli.main([
            "--snapshot",
            str(snapshot),
            "--output",
            str(output),
            "--config",
            str(config),
        ])

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

    status = benchmark_cli.main([
        "--snapshot",
        str(snapshot),
        "--output",
        str(output),
        "--config",
        str(write_fast_config(tmp_path)),
    ])

    assert status == 2, "a destructive output path is a usage failure"
    assert "must not overwrite the snapshot" in capsys.readouterr().err, (
        "the refusal names the reason"
    )
    assert snapshot.read_bytes() == before, "the snapshot is untouched"


def test_benchmark_reports_a_missing_snapshot(tmp_path: Path) -> None:
    """A run without a snapshot fails clearly instead of inventing history."""
    status = benchmark_cli.main([
        "--snapshot",
        str(tmp_path / "absent.jsonl"),
        "--output",
        str(tmp_path / "out"),
    ])

    assert status == 1, "missing snapshot is fatal"


def test_benchmark_reports_an_invalid_config(tmp_path: Path) -> None:
    """An unusable parameter file is a usage failure."""
    snapshot = tmp_path / "history.jsonl"
    snapshot.write_text("{}", encoding="utf-8")
    bad = tmp_path / "config.json"
    bad.write_text('{"config_version": 1}\n', encoding="utf-8")

    status = benchmark_cli.main([
        "--snapshot",
        str(snapshot),
        "--output",
        str(tmp_path / "out"),
        "--config",
        str(bad),
    ])

    assert status == 2, "invalid config is a usage failure"


def test_benchmark_rejects_an_unknown_option() -> None:
    """Argparse exits, and the command reports it as a usage failure."""
    assert benchmark_cli.main(["--no-such-option"]) == 2, "usage failure"


@pytest.mark.parametrize(
    ("which", "run"),
    [
        pytest.param(lambda _name: None, None, id="no-git-binary"),
        pytest.param(
            lambda _name: "/usr/bin/git",
            mock.Mock(side_effect=OSError("git unavailable")),
            id="git-call-fails",
        ),
    ],
)
def test_git_sha_is_unknown_when_git_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
    which: cabc.Callable[[str], str | None],
    run: object,
) -> None:
    """A host without a working git still produces a report."""
    _git(monkeypatch, which, run)

    marker = benchmark_cli._git_sha()

    assert marker == "unknown", "explicit marker"


def _repo_root() -> Path:
    """Return the repository root that holds the installed benchmark module."""
    return Path(benchmark_cli.__file__).resolve().parents[2]


def _answering_git(calls: list[list[str]], root: Path) -> cabc.Callable[..., object]:
    """Return a fake git that reports one commit and the supplied root."""

    def answer(command: list[str], **_options: object) -> object:
        """Record the git invocation and report a commit."""
        calls.append(command)
        return mock.Mock(stdout=f"abc123\n{root}\n", returncode=0)

    return answer


def test_git_sha_names_the_benchmark_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The commit comes from the repository holding the benchmark code.

    Reading the working directory's HEAD would record an unrelated commit
    whenever a report is produced from another checkout.
    """
    calls: list[list[str]] = []
    _git(
        monkeypatch,
        lambda _name: "/usr/bin/git",
        _answering_git(calls, _repo_root()),
    )

    commit = benchmark_cli._git_sha()

    assert commit == "abc123", "the commit is reported"
    assert calls[0][1] == "-C", "git is pointed at an explicit directory"
    assert calls[0][2].endswith("buildlimits/predict"), "the benchmark's own source"


def test_git_sha_ignores_an_untracked_copy_at_the_canonical_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file at the canonical path is not a module unless Git tracks it."""
    root = tmp_path / "host"
    module = root / "buildlimits" / "predict" / Path(benchmark_cli.__file__).name
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")

    def answer(command: list[str], **_options: object) -> object:
        """Report a commit and a root that does not track the module."""
        tracked = "ls-files" in command
        return mock.Mock(stdout=f"abc123\n{root}\n", returncode=1 if tracked else 0)

    monkeypatch.setattr(benchmark_cli, "__file__", str(module))
    _git(monkeypatch, lambda _name: "/usr/bin/git", answer)

    assert benchmark_cli._git_sha() == "unknown", "provenance is refused"


def test_git_sha_ignores_a_repository_that_does_not_own_the_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An installed copy must not borrow its host project's commit.

    A virtual environment inside another project sits in that project's
    repository, so a bare ``git -C`` would report the host's HEAD.
    """
    calls: list[list[str]] = []
    _git(
        monkeypatch,
        lambda _name: "/usr/bin/git",
        _answering_git(calls, tmp_path),
    )

    assert benchmark_cli._git_sha() == "unknown", "provenance is refused"
