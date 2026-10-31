"""Collection rules for the opt-in sccache host acceptance check."""

import dataclasses as dcs
import os
import subprocess
import sys

import pytest
import sccache_host

_LINUX_REASON = "the host check requires Linux /proc and SO_PEERCRED"
_OPT_IN_REASON = "set BUILD_LIMITS_SCCACHE_HOST_CHECK=1 to run the host check"
_MISSING_BINARY_REASON = "sccache is not installed"
_SUPPORTED_VERSION_REASON = "the host check requires a supported sccache version"
_VERSION_ERROR_REASON = "could not verify the sccache version"


@dcs.dataclass(frozen=True, slots=True)
class _HostMarkCase:
    """Describe one host-check condition and its expected collection reason."""

    platform: str
    is_enabled: bool
    executable: str | None
    version_result: subprocess.CompletedProcess[str] | Exception | None
    expected_reason: str | None


def _assert_skip_reason(
    marks: list[pytest.MarkDecorator], expected_reason: str | None
) -> None:
    """Assert the collection skip reason, or that no skip marker is present.

    Example
    -------
    >>> _assert_skip_reason(
    ...     [pytest.mark.skipif(condition=True, reason="unavailable")],
    ...     "unavailable",
    ... )
    """
    skip_markers = [mark for mark in marks if mark.name.startswith("skipif")]
    if expected_reason is None:
        assert not skip_markers, "a supported binary should permit the host check"
        return

    assert len(skip_markers) == 1, "the host check should receive one skip marker"
    assert skip_markers[0].kwargs == {
        "condition": True,
        "reason": expected_reason,
    }, "collection should explain the exact reason for skipping"


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _HostMarkCase("darwin", True, "/fake/sccache", None, _LINUX_REASON),
            id="non-linux",
        ),
        pytest.param(
            _HostMarkCase("linux", False, "/fake/sccache", None, _OPT_IN_REASON),
            id="opt-in",
        ),
        pytest.param(
            _HostMarkCase("linux", True, None, None, _MISSING_BINARY_REASON),
            id="missing-binary",
        ),
        pytest.param(
            _HostMarkCase(
                "linux",
                True,
                "/fake/sccache",
                subprocess.CompletedProcess(
                    ["/fake/sccache", "--version"], 1, "", "version command failed"
                ),
                _SUPPORTED_VERSION_REASON,
            ),
            id="version-command-failed",
        ),
        pytest.param(
            _HostMarkCase(
                "linux",
                True,
                "/fake/sccache",
                subprocess.CompletedProcess(
                    ["/fake/sccache", "--version"], 0, "sccache 0.13.0\n", ""
                ),
                _SUPPORTED_VERSION_REASON,
            ),
            id="wrong-version",
        ),
        pytest.param(
            _HostMarkCase(
                "linux",
                True,
                "/fake/sccache",
                OSError("cannot execute sccache"),
                _VERSION_ERROR_REASON,
            ),
            id="version-oserror",
        ),
        pytest.param(
            _HostMarkCase(
                "linux",
                True,
                "/fake/sccache",
                subprocess.TimeoutExpired(["/fake/sccache", "--version"], timeout=10),
                _VERSION_ERROR_REASON,
            ),
            id="version-timeout",
        ),
        pytest.param(
            _HostMarkCase(
                "linux",
                True,
                "/fake/sccache",
                subprocess.CompletedProcess(
                    ["/fake/sccache", "--version"], 0, "sccache 0.14.0\n", ""
                ),
                None,
            ),
            id="supported-version",
        ),
    ],
)
def test_sccache_host_marks_require_opt_in_and_supported_binary(
    monkeypatch: pytest.MonkeyPatch,
    case: _HostMarkCase,
) -> None:
    """Skip unsupported hosts and permit only a supported binary version."""
    monkeypatch.setattr(sys, "platform", case.platform)
    if case.is_enabled:
        monkeypatch.setitem(os.environ, "BUILD_LIMITS_SCCACHE_HOST_CHECK", "1")
    else:
        monkeypatch.delenv("BUILD_LIMITS_SCCACHE_HOST_CHECK", raising=False)

    lookups: list[str] = []

    def find_binary(name: str) -> str | None:
        """Return the test executable while recording availability checks."""
        lookups.append(name)
        return case.executable

    version_calls: list[tuple[list[str], dict[str, object]]] = []

    def run_version(
        arguments: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        """Return or raise the configured version-probe result."""
        version_calls.append((arguments, options))
        match case.version_result:
            case Exception() as error:
                raise error
            case subprocess.CompletedProcess() as result:
                return result
            case _:
                message = "the configured case must provide a version result"
                raise TypeError(message)

    monkeypatch.setattr(sccache_host.shutil, "which", find_binary)
    monkeypatch.setattr(sccache_host.subprocess, "run", run_version)

    marks = sccache_host.sccache_host_marks()
    _assert_skip_reason(marks, case.expected_reason)

    should_lookup = case.platform == "linux" and case.is_enabled
    assert lookups == (["sccache"] if should_lookup else []), (
        "binary lookup follows platform and opt-in checks"
    )
    should_check_version = should_lookup and case.executable is not None
    if should_check_version:
        assert case.executable is not None, "the selected binary has a path"
        assert len(version_calls) == 1, "the version probe runs exactly once"
        arguments, options = version_calls[0]
        assert arguments == [case.executable, "--version"], (
            "the version probe uses the selected executable"
        )
        option_names = {"check", "capture_output", "text", "timeout"}
        assert options.keys() == option_names, "the version probe options changed"
        assert options["check"] is False, "version probing must not raise on bad output"
        assert options["capture_output"] is True, "version output is captured"
        assert options["text"] is True, "version output is decoded as text"
        assert options["timeout"] == 10, "version probing has a bounded timeout"
    else:
        assert not version_calls, "version probing follows successful binary lookup"


def test_sccache_host_marks_follow_the_supported_version_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate the host check on the recorder's supported version set.

    A version added to ``SUPPORTED_SCCACHE_VERSIONS`` must make the check run
    against that binary, so support cannot be extended while the real-binary
    test still skips.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(os.environ, "BUILD_LIMITS_SCCACHE_HOST_CHECK", "1")
    monkeypatch.setattr(sccache_host.shutil, "which", lambda _name: "/fake/sccache")
    monkeypatch.setattr(
        sccache_host, "SUPPORTED_SCCACHE_VERSIONS", frozenset({"9.9.9"})
    )
    reported = {"version": "sccache 0.14.0\n"}

    def run_version(
        arguments: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        """Report the configured version string."""
        return subprocess.CompletedProcess(arguments, 0, reported["version"], "")

    monkeypatch.setattr(sccache_host.subprocess, "run", run_version)

    unsupported = sccache_host.sccache_host_marks()
    _assert_skip_reason(unsupported, _SUPPORTED_VERSION_REASON)

    reported["version"] = "sccache 9.9.9\n"
    _assert_skip_reason(sccache_host.sccache_host_marks(), None)
