"""Commit provenance for the benchmark report.

The commit is read from the repository that holds the benchmark module, never
from the working directory a report happens to be produced in. These contracts
cover the cases where no commit can be vouched for.
"""

import typing as typ
from pathlib import Path
from unittest import mock

import pytest

from buildlimits.predict import benchmark_cli

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _git(
    monkeypatch: pytest.MonkeyPatch,
    which: cabc.Callable[[str], str | None],
    run: object = None,
) -> None:
    """Make ``git`` discovery and, when supplied, its invocation answer predictably."""
    monkeypatch.setattr(benchmark_cli.shutil, "which", which)
    if run is not None:
        monkeypatch.setattr(benchmark_cli.subprocess, "run", run)


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


def _assert_unknown_provenance() -> None:
    """Assert the benchmark refuses to name a commit it cannot vouch for."""
    assert benchmark_cli._git_sha() == "unknown", "provenance is refused"


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
    assert (
        Path(calls[0][2]).resolve() == Path(benchmark_cli.__file__).resolve().parent
    ), "the benchmark's own source directory"


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

    _assert_unknown_provenance()
    assert module.is_file(), "the module exists; only its tracking is missing"


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

    _assert_unknown_provenance()
    assert not _repo_root().is_relative_to(tmp_path), (
        "the module lives outside the repository git reported"
    )
