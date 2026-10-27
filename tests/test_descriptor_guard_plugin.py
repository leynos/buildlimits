"""The leak guard reaches package doctests, not only the ``tests/`` tree."""

import textwrap
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import pytest

pytest_plugins = ["pytester"]

_TESTS = Path(__file__).parent

_LEAKING_DOCTEST = textwrap.dedent('''
    """Module whose doctest keeps a selector alive in a reference cycle.

    >>> import selectors
    >>> cycle = []
    >>> cycle.extend((cycle, selectors.DefaultSelector()))
    >>> del cycle
    """
''')


def test_a_leaking_doctest_fails_at_teardown(pytester: pytest.Pytester) -> None:
    """A doctest outside ``tests/`` is charged for the descriptor it leaks.

    The project is a throwaway tree with no conftest, which is where the
    package's own doctests live relative to the guard's registration.
    """
    pytester.makeini(
        f"""
        [pytest]
        pythonpath = {_TESTS}
        addopts = --doctest-modules -p descriptor_guard
        """
    )
    pytester.makepyfile(leaky=_LEAKING_DOCTEST)
    # The parent coverage run's SlipCover plugin is inherited by this child.
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-p", "no:slipcover"
    )
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*left 1 descriptor(s) open*eventpoll*"])
