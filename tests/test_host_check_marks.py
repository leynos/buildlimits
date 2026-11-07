"""Collection rules for the opt-in cache-backend host checks.

Both backends' marks come from one rule, :func:`host_check_marks.host_check_marks`,
so this module runs that rule against each backend's own parameters: its
marker, environment variable, binary name, supported version set and reasons.
A backend wired to the wrong parameter is caught either by the skip reason that
results or by the binary lookup that does not happen.
"""

import dataclasses as dcs
import os
import subprocess
import sys
import typing as typ

import host_check_marks
import kache_host
import pytest
import sccache_host

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types


@dcs.dataclass(frozen=True, slots=True)
class _VersionRow:
    """Name one extra version reading a single backend needs covered."""

    identifier: str
    stdout: str


@dcs.dataclass(frozen=True, slots=True)
class _Backend:
    """Identify one backend's host-check collection parameters.

    The reasons are restated here rather than imported, so a message that
    drifts from what the operator is told is a test failure rather than a
    tautology.
    """

    module: types.ModuleType
    marks: cabc.Callable[[], list[pytest.MarkDecorator]]
    versions_attribute: str
    opt_in_variable: str
    binary: str
    platform_reason: str
    opt_in_reason: str
    missing_reason: str
    unsupported_reason: str
    version_error_reason: str
    supported_version: str
    unsupported_version: str
    supported_extras: tuple[_VersionRow, ...] = ()
    unsupported_extras: tuple[_VersionRow, ...] = ()


_KACHE = _Backend(
    module=kache_host,
    marks=kache_host.kache_host_marks,
    versions_attribute="SUPPORTED_KACHE_VERSIONS",
    opt_in_variable="BUILD_LIMITS_KACHE_HOST_CHECK",
    binary="kache",
    platform_reason="the kache host check is only supported on Linux",
    opt_in_reason="set BUILD_LIMITS_KACHE_HOST_CHECK=1 to run the host check",
    missing_reason="kache is not installed",
    unsupported_reason="the host check requires a supported kache version",
    version_error_reason="could not verify the kache version",
    supported_version="kache 1.2.2\n",
    unsupported_version="kache 1.1.0\n",
    supported_extras=(
        _VersionRow("supported-release-candidate", "kache 2.0.0-rc.1\n"),
    ),
    unsupported_extras=(_VersionRow("unprefixed-version", "1.2.2\n"),),
)

_SCCACHE = _Backend(
    module=sccache_host,
    marks=sccache_host.sccache_host_marks,
    versions_attribute="SUPPORTED_SCCACHE_VERSIONS",
    opt_in_variable="BUILD_LIMITS_SCCACHE_HOST_CHECK",
    binary="sccache",
    platform_reason="the host check requires Linux /proc and SO_PEERCRED",
    opt_in_reason="set BUILD_LIMITS_SCCACHE_HOST_CHECK=1 to run the host check",
    missing_reason="sccache is not installed",
    unsupported_reason="the host check requires a supported sccache version",
    version_error_reason="could not verify the sccache version",
    supported_version="sccache 0.14.0\n",
    unsupported_version="sccache 0.13.0\n",
)


@dcs.dataclass(frozen=True, slots=True)
class _HostMarkCase:
    """Describe one host-check condition and its expected collection reason."""

    name: str
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


def _cases(backend: _Backend) -> list[_HostMarkCase]:
    """Build the version-probe cases for one backend.

    Returns
    -------
    list[_HostMarkCase]
        Every condition under which collection permits or refuses the check.
    """
    binary = backend.binary
    executable = f"/fake/{binary}"

    def completed(returncode: int, stdout: str) -> subprocess.CompletedProcess[str]:
        """Build a version-probe result for this backend's binary."""
        return subprocess.CompletedProcess(
            [executable, "--version"], returncode, stdout, ""
        )

    return [
        _HostMarkCase(
            "non-linux", "darwin", True, executable, None, backend.platform_reason
        ),
        _HostMarkCase(
            "opt-in", "linux", False, executable, None, backend.opt_in_reason
        ),
        _HostMarkCase(
            "missing-binary", "linux", True, None, None, backend.missing_reason
        ),
        _HostMarkCase(
            "version-command-failed",
            "linux",
            True,
            executable,
            completed(1, ""),
            backend.unsupported_reason,
        ),
        _HostMarkCase(
            "wrong-version",
            "linux",
            True,
            executable,
            completed(0, backend.unsupported_version),
            backend.unsupported_reason,
        ),
        _HostMarkCase(
            "version-oserror",
            "linux",
            True,
            executable,
            OSError(f"cannot execute {binary}"),
            backend.version_error_reason,
        ),
        _HostMarkCase(
            "version-timeout",
            "linux",
            True,
            executable,
            subprocess.TimeoutExpired([executable, "--version"], timeout=10),
            backend.version_error_reason,
        ),
        _HostMarkCase(
            "supported-version",
            "linux",
            True,
            executable,
            completed(0, backend.supported_version),
            None,
        ),
        *(
            _HostMarkCase(
                row.identifier,
                "linux",
                True,
                executable,
                completed(0, row.stdout),
                None,
            )
            for row in backend.supported_extras
        ),
        *(
            _HostMarkCase(
                row.identifier,
                "linux",
                True,
                executable,
                completed(0, row.stdout),
                backend.unsupported_reason,
            )
            for row in backend.unsupported_extras
        ),
    ]


#: Every backend's cases in one table, so each row's id names its backend.
_HOST_MARK_CASES = [
    pytest.param(backend, case, id=f"{backend.binary}/{case.name}")
    for backend in (_KACHE, _SCCACHE)
    for case in _cases(backend)
]


@pytest.mark.parametrize(("backend", "case"), _HOST_MARK_CASES)
def test_host_marks_require_opt_in_and_supported_binary(
    monkeypatch: pytest.MonkeyPatch,
    backend: _Backend,
    case: _HostMarkCase,
) -> None:
    """Skip unsupported hosts and permit only a supported binary version."""
    monkeypatch.setattr(sys, "platform", case.platform)
    if case.is_enabled:
        monkeypatch.setitem(os.environ, backend.opt_in_variable, "1")
    else:
        monkeypatch.delenv(backend.opt_in_variable, raising=False)

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

    monkeypatch.setattr(host_check_marks.shutil, "which", find_binary)
    monkeypatch.setattr(host_check_marks.subprocess, "run", run_version)

    _assert_skip_reason(backend.marks(), case.expected_reason)

    should_lookup = case.platform == "linux" and case.is_enabled
    expected_lookups = [backend.binary] if should_lookup else []
    assert lookups == expected_lookups, (
        "binary lookup follows platform and opt-in checks"
    )
    should_check_version = should_lookup and case.executable is not None
    if not should_check_version:
        assert not version_calls, "version probing follows successful binary lookup"
        return
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


@pytest.mark.parametrize("backend", [_KACHE, _SCCACHE], ids=lambda b: b.binary)
def test_host_marks_follow_the_backend_supported_version_set(
    monkeypatch: pytest.MonkeyPatch, backend: _Backend
) -> None:
    """Gate the host check on the backend's own supported version set.

    A version added to that set must make the check run against that binary, so
    support cannot be extended while the real-binary test still skips.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(os.environ, backend.opt_in_variable, "1")
    monkeypatch.setattr(
        host_check_marks.shutil, "which", lambda _name: f"/fake/{backend.binary}"
    )
    monkeypatch.setattr(
        backend.module, backend.versions_attribute, frozenset({"9.9.9"})
    )
    reported = {"version": backend.supported_version}

    def run_version(
        arguments: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        """Report the configured version string."""
        return subprocess.CompletedProcess(arguments, 0, reported["version"], "")

    monkeypatch.setattr(host_check_marks.subprocess, "run", run_version)

    _assert_skip_reason(backend.marks(), backend.unsupported_reason)

    reported["version"] = f"{backend.binary} 9.9.9\n"
    _assert_skip_reason(backend.marks(), None)
