"""Compose the collection marks every opt-in cache-backend host check shares.

sccache and kache are checked the same way: a Linux-only test module that runs
only when the operator opts in and the backend binary on ``PATH`` reports a
version the check has been verified against. What differs is the marker name,
the environment variable, and the supported version set; everything around them
-- the platform guard, the opt-in gate, the version probe and its three failure
readings -- is one rule and lives here.

A backend with a genuinely different collection rule should not bend this
module to fit. It should carry its own, as kache would have to if a release
ever moved version reporting off ``--version``.
"""

import dataclasses as dc
import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell.
import sys
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: ``--version`` answers immediately on every backend checked so far; this only
#: bounds a binary that would otherwise wedge collection.
VERSION_TIMEOUT_SECONDS = 10.0


def end_process(process: subprocess.Popen[bytes], timeout: float) -> None:
    """End a private backend process, bounding both the polite and forced wait.

    The caller has already asked the backend to stop through its own client, so
    reaching here means a process that would not stop, or one whose client
    command cannot be trusted to stop it. Terminating first keeps the ordinary
    case clean; the kill is what makes teardown bounded.

    Parameters
    ----------
    process : subprocess.Popen[bytes]
        The backend process to end.
    timeout : float
        Seconds to allow each of the terminate and kill waits.
    """
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def decoded_document(stdout: str, *, source: str) -> dict[str, object]:
    """Decode one backend document that the caller requires to be an object.

    Both backends answer a statistics query with a JSON object at the top
    level. A list, a string or ``null`` there means the backend changed shape,
    and continuing would report a missing counter rather than the real cause.

    Parameters
    ----------
    stdout : str
        The command's captured standard output.
    source : str
        The backend name used in the error, such as ``"kache"``.

    Returns
    -------
    dict[str, object]
        The decoded document.

    Raises
    ------
    TypeError
        If the document is not a JSON object.

    Examples
    --------
    >>> decoded_document('{"daemon_running": true}', source="kache")
    {'daemon_running': True}
    >>> decoded_document("[]", source="kache")
    Traceback (most recent call last):
    ...
    TypeError: kache returned a non-object statistics document
    """
    value = json.loads(stdout)
    if not isinstance(value, dict):
        message = f"{source} returned a non-object statistics document"
        raise TypeError(message)
    return typ.cast("dict[str, object]", value)


@dc.dataclass(frozen=True, slots=True)
class HostCheck:
    """Identify one backend's host-check collection rule.

    The three reasons are all about the binary, so they travel with it rather
    than with the module that happens to call this one: a skip message should
    name the executable the operator has to install or upgrade.
    """

    marker: pytest.MarkDecorator
    """The ``pytest.mark`` that selects this backend's tests by name."""

    opt_in_variable: str
    """The environment variable that must read ``1`` to run the check."""

    binary: str
    """The executable expected on ``PATH``."""

    versions: cabc.Iterable[str]
    """The releases the check has been verified against."""

    unavailable_reason: str
    """Why a binary reporting an unsupported version is refused."""

    version_error_reason: str
    """Why a binary that will not answer ``--version`` is refused."""


def host_check_marks(
    check: HostCheck,
    *,
    platform_reason: str,
    opt_in_reason: str,
) -> list[pytest.MarkDecorator]:
    """Mark a Linux-only opt-in test and skip hosts without a supported binary.

    The platform and opt-in reasons are the caller's because they explain this
    backend's own prerequisites; the version reasons are the check's because
    they all describe the same binary.

    Parameters
    ----------
    check : HostCheck
        The backend's marker, environment variable, binary and versions.
    platform_reason : str
        Why the check is limited to Linux.
    opt_in_reason : str
        How to opt in.

    Returns
    -------
    list[pytest.MarkDecorator]
        The opt-in and version-availability marks, in collection order.
    """
    marks = [check.marker, pytest.mark.timeout(600)]
    if sys.platform != "linux":
        marks.append(pytest.mark.skipif(condition=True, reason=platform_reason))
        return marks
    if os.environ.get(check.opt_in_variable) != "1":
        marks.append(pytest.mark.skipif(condition=True, reason=opt_in_reason))
        return marks

    executable = shutil.which(check.binary)
    if executable is None:
        reason = f"{check.binary} is not installed"
    else:
        try:
            # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
            version = subprocess.run(
                [executable, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=VERSION_TIMEOUT_SECONDS,
            )
        except OSError, subprocess.TimeoutExpired:
            reason = check.version_error_reason
        else:
            reason = _version_refusal(check, version)
    if reason:
        marks.append(pytest.mark.skipif(condition=True, reason=reason))
    return marks


def _version_refusal(
    check: HostCheck, version: subprocess.CompletedProcess[str]
) -> str:
    """Return why the reported version cannot run the host check.

    The gate follows the caller's supported set rather than one literal
    version, so adding a release there makes the check run against that binary
    instead of silently skipping it. Every backend so far prints its name
    before the version, so the comparison includes that prefix.

    Parameters
    ----------
    check : HostCheck
        The backend whose supported set decides the answer.
    version : subprocess.CompletedProcess[str]
        The result of asking the selected binary for its version.

    Returns
    -------
    str
        An empty string when the host check may run, otherwise the skip
        reason.
    """
    supported = {f"{check.binary} {entry}" for entry in check.versions}
    if version.returncode == 0 and version.stdout.strip() in supported:
        return ""
    return check.unavailable_reason
