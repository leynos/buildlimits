"""``--fail-on-skip`` makes a skipped model test a failure under the opt-in.

D6 requires ``make model-check`` to fail when anything was skipped, so a test
that skipped itself cannot be counted as a pass. The rule is a pytest option
rather than a session-wide default, because ``make test`` must keep running
suites that skip when an optional tool or privilege is absent. Both halves are
pinned here: with the flag a skip fails the session, and without it the skip is
ordinary.
"""

import textwrap
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_TESTS = Path(__file__).parent

_SKIP_NOTICE = "*--fail-on-skip: 1 test(s) skipped*"
"""The line a flagged session prints once it has recorded the skip."""

_CHECKS = textwrap.dedent('''
    """Two selected tests: one skips itself, one passes."""

    import pytest


    def test_skipped() -> None:
        """Skip, as a model test would when its prerequisite is missing."""
        pytest.skip("a prerequisite is absent")


    def test_passes() -> None:
        """Pass, so the skip alone decides whether the session succeeds."""
        assert True
    ''')


_PASSING = textwrap.dedent('''
    """One passing test, so a collection skip alone decides the session."""


    def test_passes() -> None:
        """Pass, so only the collection skip can fail the session."""
        assert True
    ''')

_SKIPPED_MODULE = textwrap.dedent('''
    """A module that skips before any test function is collected."""

    import pytest

    pytest.skip("the optional tool is absent", allow_module_level=True)
    ''')


def _configured(pytester: pytest.Pytester) -> None:
    """Give the throwaway project the ini file that loads the plugin.

    Parameters
    ----------
    pytester : pytest.Pytester
        The fixture that owns the throwaway project.
    """
    pytester.makeini(f"""
        [pytest]
        pythonpath = {_TESTS}
        addopts = -p skip_guard
        """)


def _run(pytester: pytest.Pytester, *arguments: str) -> pytest.RunResult:
    """Run the throwaway project's files and return the finished session.

    Parameters
    ----------
    pytester : pytest.Pytester
        The fixture that owns the throwaway project.
    *arguments : str
        Paths and command line arguments, in the order pytest should see them.

    Returns
    -------
    pytest.RunResult
        The finished inner session.
    """
    # The parent coverage run's SlipCover plugin is inherited by this child.
    return pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-p", "no:slipcover", *arguments
    )


def _session(pytester: pytest.Pytester, *arguments: str) -> pytest.RunResult:
    """Run the two check tests in a throwaway project that loads the plugin.

    Parameters
    ----------
    pytester : pytest.Pytester
        The fixture that owns the throwaway project.
    *arguments : str
        Extra command line arguments, which is where the flag under test goes.

    Returns
    -------
    pytest.RunResult
        The finished inner session.
    """
    _configured(pytester)
    pytester.makepyfile(checks=_CHECKS)
    return _run(pytester, "checks.py", *arguments)


def _collection_session(pytester: pytest.Pytester, *arguments: str) -> pytest.RunResult:
    """Run a passing file beside one that skips during collection.

    Parameters
    ----------
    pytester : pytest.Pytester
        The fixture that owns the throwaway project.
    *arguments : str
        Extra command line arguments, which is where the flag under test goes.

    Returns
    -------
    pytest.RunResult
        The finished inner session.
    """
    _configured(pytester)
    pytester.makepyfile(passing=_PASSING, skipped_module=_SKIPPED_MODULE)
    return _run(pytester, "passing.py", "skipped_module.py", *arguments)


def _assert_session(
    result: pytest.RunResult,
    status: pytest.ExitCode,
    reason: str,
    notice: str | None = None,
) -> None:
    """Assert one pass and one skip left the session with the expected status.

    Parameters
    ----------
    result : pytest.RunResult
        The finished inner session.
    status : pytest.ExitCode
        The exit code the skip should have produced.
    reason : str
        What the status is attributed to, for the assertion message.
    notice : str | None
        A line the session must have printed, when the flag reports the skip.
    """
    result.assert_outcomes(passed=1, skipped=1)
    assert result.ret == status, reason
    if notice is not None:
        result.stdout.fnmatch_lines([notice])


def test_a_skipped_test_fails_the_checker_run(pytester: pytest.Pytester) -> None:
    """A skip under the opt-in is a failure, so the matrix cannot pass unrun.

    The passing test is what makes this a test of the skip: without it, a
    session that collected nothing would also exit non-zero.
    """
    _assert_session(
        _session(pytester, "--fail-on-skip"),
        pytest.ExitCode.TESTS_FAILED,
        "the skipped test fails the session",
        _SKIP_NOTICE,
    )


def test_a_skipped_test_is_ordinary_without_the_flag(pytester: pytest.Pytester) -> None:
    """The ordinary suite still runs suites that skip for their environment."""
    _assert_session(
        _session(pytester),
        pytest.ExitCode.OK,
        "an unflagged skip does not fail the run",
    )


def test_a_module_skipped_at_collection_fails_the_checker_run(
    pytester: pytest.Pytester,
) -> None:
    """A file skipped while collecting is a failure, not a silent omission.

    A module-level skip produces no test report at all, so the per-test hook
    never sees it. Without collection handling the session would exit 0 with a
    whole file unrun, which is the silent success D6 exists to prevent.
    """
    _assert_session(
        _collection_session(pytester, "--fail-on-skip"),
        pytest.ExitCode.TESTS_FAILED,
        "the collection-time skip fails the session",
        _SKIP_NOTICE,
    )


def test_a_module_skipped_at_collection_is_ordinary_without_the_flag(
    pytester: pytest.Pytester,
) -> None:
    """The ordinary suite still tolerates a module that cannot run here."""
    _assert_session(
        _collection_session(pytester),
        pytest.ExitCode.OK,
        "an unflagged collection skip does not fail the run",
    )
