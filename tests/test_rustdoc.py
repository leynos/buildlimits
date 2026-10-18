"""Tests for finding the real rustdoc through the provisioned rustup."""

import subprocess
import sys
import typing as typ

import pytest

from buildlimits import rustdoc
from buildlimits.rustdoc import RustdocError, resolve_rustdoc

if typ.TYPE_CHECKING:
    import collections.abc as cabc

ENV = {"BUILD_LIMITS_RUSTUP": "/opt/rustup/bin/rustup"}


def _answering(
    returncode: int, stdout: str, stderr: str = ""
) -> cabc.Callable[
    [cabc.Sequence[str], str, dict[str, str]], subprocess.CompletedProcess[str]
]:
    """Build a rustup runner that records its call and gives one answer."""

    def run(
        argv: cabc.Sequence[str], cwd: str, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        """Answer ``rustup which rustdoc`` from the script."""
        assert list(argv) == [ENV["BUILD_LIMITS_RUSTUP"], "which", "rustdoc"], argv
        assert cwd == "/work", "toolchain overrides come from the caller's directory"
        assert env == ENV, "rustup runs in the caller's environment"
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return run


def test_find_rustup_prefers_the_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BUILD_LIMITS_RUSTUP`` is used without consulting ``PATH``."""

    def fail_which(*_args: object, **_kwargs: object) -> None:
        """Fail the test if PATH lookup is attempted."""
        msg = "shutil.which must not be called"
        raise AssertionError(msg)

    monkeypatch.setattr(rustdoc.shutil, "which", fail_which)
    assert rustdoc._find_rustup(ENV) == ENV["BUILD_LIMITS_RUSTUP"], (
        "the explicit override wins"
    )


@pytest.mark.parametrize(
    ("found", "expected"), [("/usr/bin/rustup", "/usr/bin/rustup"), (None, None)]
)
def test_find_rustup_searches_the_callers_path(
    monkeypatch: pytest.MonkeyPatch, found: str | None, expected: str | None
) -> None:
    """Without an override, rustup comes from ``PATH``, or is an error."""
    monkeypatch.setattr(rustdoc.shutil, "which", lambda name, path=None: found)
    if expected is None:
        with pytest.raises(RustdocError, match="cannot find rustup"):
            rustdoc._find_rustup({})
    else:
        assert rustdoc._find_rustup({}) == expected, "PATH lookup is used"


def test_resolve_rustdoc_returns_the_resolved_path() -> None:
    """A successful ``rustup which rustdoc`` yields the resolved path."""
    run = _answering(0, "/real/rustdoc\n")
    assert resolve_rustdoc(ENV, "/work", run) == "/real/rustdoc", (
        "the resolved rustdoc path is returned"
    )


@pytest.mark.parametrize(
    ("returncode", "stdout", "message"),
    [
        (1, "", "no such toolchain"),
        (0, sys.argv[0] + "\n", "resolved rustdoc to this adapter"),
    ],
    ids=["rustup-failure", "self-resolution"],
)
def test_resolve_rustdoc_refuses_an_unusable_answer(
    returncode: int, stdout: str, message: str
) -> None:
    """A failed lookup, or one that finds this adapter, is a rustdoc error."""
    run = _answering(returncode, stdout, "no such toolchain")
    with pytest.raises(RustdocError, match=message):
        resolve_rustdoc(ENV, "/work", run)


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError(13, "Permission denied", "/opt/rustup/bin/rustup"),
        subprocess.TimeoutExpired(["rustup"], 30),
    ],
    ids=["not-executable", "timeout"],
)
def test_a_rustup_that_cannot_run_is_a_rustdoc_error(failure: Exception) -> None:
    """Start-up and timeout failures are reported, never left as tracebacks."""

    def broken(
        argv: cabc.Sequence[str], cwd: str, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        """Fail as running rustup would."""
        raise failure

    with pytest.raises(RustdocError, match="cannot run rustup") as caught:
        resolve_rustdoc(ENV, "/work", broken)
    assert caught.value.__cause__ is failure, "the original failure is the cause"
