"""Each configuration query reads the model directory and changes nothing.

:mod:`tests.test_modelcheck_configuration` says what each query returns and
what it refuses. What it cannot say is the boundary the adapter relies on to be
usable at all: the queries are reads, so a run may call them over a checkout
without rewriting a file, creating one, or starting a process that could do
either. Those are properties of the adapter's implementation rather than of the
values it produces, so they are asserted here against the directory itself,
before and after each query.

The guard is proved live before the query runs. A monkeypatched process
function that nothing calls would let this module pass while the adapter
started a shell behind it, so each test first asks the guard to refuse a call
the test makes itself. Only then is the query allowed to run.
"""

import collections.abc as cabc
import os
import subprocess
import typing as typ
from pathlib import Path

import pytest
from directory_state_test_helpers import snapshot_tree
from modelcheck_configuration_support import model_directory

from modelcheck.adapters.configuration import TomlConfigurations

_CONFIGURATIONS = """\
[configurations.head]
min_states = 10
holds = ["S1"]
reachable = ["StopIssued"]
fails_as = { L3 = "lasso" }

[exclusions]
"buildlimits.effects.Pause" = "modelled as an unobservable effect"
"""
"""A file declaring both a configuration and an exclusion, so every query reads."""


def _refuse(*_args: object, **_kwargs: object) -> typ.NoReturn:
    """Refuse a call the adapter was not allowed to make.

    Parameters
    ----------
    *_args : object
        The positional arguments of the refused call, ignored.
    **_kwargs : object
        The keyword arguments of the refused call, ignored.

    Raises
    ------
    AssertionError
        Always. The query under test may only read the directory.
    """
    raise AssertionError("the configuration adapter started a process")


@pytest.mark.parametrize(
    "query",
    [
        TomlConfigurations.source_text,
        TomlConfigurations.configurations,
        TomlConfigurations.exclusions,
    ],
    ids=["source", "configurations", "exclusions"],
)
def test_a_query_reads_the_directory_without_changing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: cabc.Callable[[TomlConfigurations], object],
) -> None:
    """A query is a read: it changes no file and starts no process.

    The adapter is handed a real directory holding a model, a configuration and
    an exclusion, because that is what a matrix run reads. The query is the
    adapter's own method rather than a wrapper around it, so nothing stands
    between this test and the call the matrix run makes. A fresh adapter is
    built for each query, so the document cache cannot hide a second read that
    a later query would have made.
    """
    directory = model_directory(tmp_path, _CONFIGURATIONS)
    before = snapshot_tree(directory, relative_to=directory)
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _refuse)
    monkeypatch.setattr(os, "system", _refuse)
    monkeypatch.setattr(os, "popen", _refuse)

    # The guard is live, so a call the adapter made would be caught.
    with pytest.raises(AssertionError, match="started a process"):
        subprocess.run(["true"], check=False)

    query(TomlConfigurations(directory))

    after = snapshot_tree(directory, relative_to=directory)
    assert after == before, "the query rewrote, added or removed a file"
