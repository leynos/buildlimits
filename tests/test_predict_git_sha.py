"""Commit provenance for the benchmark report.

The commit is read from the repository that holds the benchmark module, never
from the working directory a report happens to be produced in. These contracts
cover the cases where no commit can be vouched for.
"""

import typing as typ
from pathlib import Path
from unittest import mock

import pytest

from buildlimits.predict import provenance

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _git(
    monkeypatch: pytest.MonkeyPatch,
    which: cabc.Callable[[str], str | None],
    run: object = None,
) -> None:
    """Make ``git`` discovery and, when supplied, its invocation answer predictably."""
    monkeypatch.setattr(provenance.shutil, "which", which)
    if run is not None:
        monkeypatch.setattr(provenance.subprocess, "run", run)


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

    marker = provenance.git_revision()

    assert marker == "unknown", "explicit marker"


def _repo_root() -> Path:
    """Return the repository root that holds the installed benchmark module."""
    return Path(provenance.__file__).resolve().parents[2]


def _assert_unknown_provenance() -> None:
    """Assert the benchmark refuses to name a commit it cannot vouch for."""
    assert provenance.git_revision() == "unknown", "provenance is refused"


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

    commit = provenance.git_revision()

    assert commit == "abc123", "the commit is reported"
    assert calls[0][1] == "-C", "git is pointed at an explicit directory"
    assert Path(calls[0][2]).resolve() == Path(provenance.__file__).resolve().parent, (
        "the benchmark's own source directory"
    )


def test_git_sha_ignores_a_copy_the_commit_does_not_hold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file at the canonical path must appear in the commit the report names.

    Asking the index instead would accept a copy that was staged but never
    committed: ``git ls-files`` lists it while ``HEAD`` has no such entry, so
    the report would name a commit that does not contain the code it ran.
    """
    root = tmp_path / "host"
    relative = Path("buildlimits") / "predict" / Path(provenance.__file__).name
    module = root / relative
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    def answer(command: list[str], **_options: object) -> object:
        """Report a commit, and a tree that does not hold the module."""
        calls.append(command)
        missing = "cat-file" in command
        return mock.Mock(stdout=f"abc123\n{root}\n", returncode=1 if missing else 0)

    monkeypatch.setattr(provenance, "__file__", str(module))
    _git(monkeypatch, lambda _name: "/usr/bin/git", answer)

    _assert_unknown_provenance()
    assert calls[-1][3:6] == ["cat-file", "-e", f"abc123:{relative}"], (
        "the commit's tree is asked, not the index"
    )
    assert module.is_file(), "the module exists; only the commit is missing it"


def test_both_git_calls_ignore_an_inherited_repository_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-C`` does not overrule ``GIT_DIR``, so the environment is scrubbed.

    With a repository selected by the environment, both calls would read that
    checkout while ``rev-parse`` still printed a root, and a module present in
    both repositories would let the other checkout's commit vouch for this
    code. Unrelated variables must survive, because the executable needs
    ``PATH`` to run.
    """
    options: list[dict[str, object]] = []

    def answer(command: list[str], **values: object) -> object:
        """Record the options and report a commit that holds the module."""
        options.append(values)
        return mock.Mock(stdout=f"abc123\n{_repo_root()}\n", returncode=0)

    # The first two select another repository; the third must survive the
    # scrub, or the test could pass by emptying the environment.
    for name, value in {
        "GIT_DIR": str(tmp_path / "elsewhere.git"),
        "GIT_WORK_TREE": str(tmp_path),
        "PREDICT_PROVENANCE_MARKER": "kept",
    }.items():
        monkeypatch.setenv(name, value)
    _git(monkeypatch, lambda _name: "/usr/bin/git", answer)

    provenance.git_revision()

    assert len(options) == 2, "the root and the tree are both asked"
    for values in options:
        environment = typ.cast("dict[str, str]", values["env"])
        assert not [name for name in environment if name.startswith("GIT_")], (
            "no inherited repository selection reaches git"
        )
        assert environment["PREDICT_PROVENANCE_MARKER"] == "kept", (
            "unrelated variables are preserved"
        )


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
