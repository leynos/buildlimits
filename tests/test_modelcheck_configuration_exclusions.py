"""An exclusion is a gap the file explains, and one it does not is refused.

An excluded path is a part of the production surface a model does not cover.
The reason beside it is what makes that gap a considered decision rather than a
forgotten one, so the adapter reads the value as text and refuses any other
shape: coercing it would let a number, or a string holding nothing, pass for an
explanation nobody wrote.

These tests live apart from :mod:`tests.test_modelcheck_configuration` so that
each module is a coherent subject and neither grows past the repository's size
limit. The model directory they read is written by that module's helper, so
both assert against one layout rather than two copies of it.
"""

from pathlib import Path

import pytest
from test_modelcheck_configuration import _directory

from modelcheck import domain
from modelcheck.adapters.configuration import TomlConfigurations


def test_exclusions_carry_their_reason(tmp_path: Path) -> None:
    """An exclusion is a considered gap, and the reason travels with it."""
    directory = _directory(
        tmp_path,
        """
[exclusions]
"buildlimits.effects.Pause" = "modelled as an unobservable effect"
""",
    )

    assert TomlConfigurations(directory).exclusions() == {
        "buildlimits.effects.Pause": "modelled as an unobservable effect"
    }, "the reason travels with the exclusion"


@pytest.mark.parametrize(
    "declared",
    ['""', '"   "', "3"],
    ids=["empty", "whitespace", "not-a-string"],
)
def test_an_exclusion_reason_that_is_not_an_explanation_is_refused(
    tmp_path: Path, declared: str
) -> None:
    """An exclusion's reason is required, and a value of another shape is not one.

    ``str`` would turn an integer into a reason nobody wrote, and an empty
    string would satisfy the field without saying why the path is absent.
    Either way the record would claim a considered decision the file does not
    contain.
    """
    directory = _directory(
        tmp_path,
        f'[exclusions]\n"buildlimits.effects.Pause" = {declared}\n',
    )

    with pytest.raises(domain.MaterializeError, match="exclusion reason"):
        TomlConfigurations(directory).exclusions()
