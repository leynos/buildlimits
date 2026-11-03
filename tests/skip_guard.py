"""Fail a checker run in which any selected test skipped (D6).

``make model-check`` exists to prove the model matrix, so a test that skips
itself would let the target report a pass it never earned. A missing
prerequisite is a failure under the opt-in, not a skip.

The rule is a pytest option rather than a session-wide default because several
suites skip legitimately when an optional tool or privilege is absent, and
``make test`` must keep running them. ``make model-check`` passes the option;
nothing else does.
"""

import pytest

__all__ = [
    "pytest_addoption",
    "pytest_collectreport",
    "pytest_runtest_logreport",
    "pytest_sessionfinish",
    "pytest_sessionstart",
]
"""The hooks pytest discovers by name, named here so dead-code checks see them."""

_SKIPPED: list[str] = []
"""The node IDs skipped in this session, in report order."""


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register ``--fail-on-skip``.

    Parameters
    ----------
    parser : pytest.Parser
        The command line parser to extend.
    """
    parser.addoption(
        "--fail-on-skip",
        action="store_true",
        default=False,
        help="fail the session if any selected test skips (used by make model-check)",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Start each session with no recorded skips.

    Parameters
    ----------
    session : pytest.Session
        Unused; present for the hook's signature.
    """
    del session
    _SKIPPED.clear()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record a skipped test, whichever phase reported it.

    Parameters
    ----------
    report : pytest.TestReport
        The report for one phase of one test.
    """
    if report.skipped:
        _SKIPPED.append(report.nodeid)


def pytest_collectreport(report: pytest.CollectReport) -> None:
    """Record a module skipped while it was being collected.

    A module that calls ``pytest.skip(..., allow_module_level=True)``, or that
    a ``pytest.importorskip`` at import time turns aside, skips before any test
    function exists to report. Without this hook such a module is invisible to
    ``--fail-on-skip``, and the session could pass with a whole file unrun —
    exactly the silent success D6 exists to prevent.

    Parameters
    ----------
    report : pytest.CollectReport
        The report for one collected file or directory.
    """
    if report.skipped:
        _SKIPPED.append(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the session when a skip was recorded and the rule is on.

    Parameters
    ----------
    session : pytest.Session
        The finishing session, whose status this may change.
    exitstatus : int
        The status pytest computed.
    """
    if not session.config.getoption("fail_on_skip") or not _SKIPPED:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            f"--fail-on-skip: {len(_SKIPPED)} test(s) skipped: " + ", ".join(_SKIPPED)
        )
    if exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
