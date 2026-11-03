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
    pytester.makeini(f"""
        [pytest]
        pythonpath = {_TESTS}
        addopts = -p skip_guard
        """)
    pytester.makepyfile(checks=_CHECKS)
    # The parent coverage run's SlipCover plugin is inherited by this child.
    return pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-p", "no:slipcover", "checks.py", *arguments
    )


def test_a_skipped_test_fails_the_checker_run(pytester: pytest.Pytester) -> None:
    """A skip under the opt-in is a failure, so the matrix cannot pass unrun.

    The passing test is what makes this a test of the skip: without it, a
    session that collected nothing would also exit non-zero.
    """
    result = _session(pytester, "--fail-on-skip")

    result.assert_outcomes(passed=1, skipped=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED, (
        "the skipped test fails the session"
    )
    result.stdout.fnmatch_lines(["*--fail-on-skip: 1 test(s) skipped*"])


def test_a_skipped_test_is_ordinary_without_the_flag(pytester: pytest.Pytester) -> None:
    """The ordinary suite still runs suites that skip for their environment."""
    result = _session(pytester)

    result.assert_outcomes(passed=1, skipped=1)
    assert result.ret == pytest.ExitCode.OK, "an unflagged skip does not fail the run"
