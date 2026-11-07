"""Read back which assertions a materialized specification still declares.

Assertion selection is judged by what survives the rewrite rather than by what
the configuration asked for, because the two differ whenever a configuration
names nothing: it then strips nothing, and the model's own annotations are what
a run carries. Both the materializer tests and the selection tests need that
readback, and writing it twice would let the two definitions disagree about a
spelling such as ``always eventually``.

This module is test support and is linted as production code, following the
convention in ``docs/repository-layout.md``: ``test_*.py`` modules hold tests,
anything else here holds helpers.
"""

import re
import typing as typ

import modelcheck_cases as cases

if typ.TYPE_CHECKING:
    from modelcheck.domain import Expectation

_ASSERTION = re.compile(
    r"^(?:always |eventually |exists )*assertion (\w+):", re.MULTILINE
)
"""The declaration line of an assertion, whatever liveness spelling precedes it."""


def kept(text: str) -> set[str]:
    """Return the names of the assertions a materialized text still declares.

    Parameters
    ----------
    text : str
        A materialized specification.

    Returns
    -------
    set[str]
        Every assertion name that survives the rewrite.

    Examples
    --------
    >>> sorted(kept(cases.TAGGED))
    ['ReachesOne', 'ReachesTen', 'UnderTen']
    """
    return set(_ASSERTION.findall(text))


def kept_by(
    expectations: tuple[Expectation, ...],
    *,
    name: str = "control",
    expected_failure: str | None = None,
) -> set[str]:
    """Return the assertions one run of :data:`cases.TAGGED` keeps.

    Parameters
    ----------
    expectations : tuple[Expectation, ...]
        What the configuration expects.
    name : str
        The configuration's name.
    expected_failure : str | None
        The one assertion the run is expected to violate, or ``None``.

    Returns
    -------
    set[str]
        The assertion names left in the materialized text.

    Examples
    --------
    >>> from modelcheck import domain
    >>> sorted(kept_by((domain.Holds("UnderTen"),)))
    ['UnderTen']
    """
    from modelcheck import materialize

    configuration = cases.configuration(name, expectations=expectations)
    spec = materialize.materialize(
        cases.TAGGED, configuration, expected_failure=expected_failure
    )
    return kept(spec.text)
