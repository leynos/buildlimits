"""Scaffolding shared by the supervisor's direct-unit test modules.

Several modules drive :class:`Supervisor` handlers directly and each starts
from the same premise: a supervisor over a systemd manager that answers every
query with nothing, at a fixed clock, with its selector closed when the test
ends. The substitution is identical in every case, so it lives here rather
than being copied between the modules.
"""

import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests script the manager's argv, never a shell.
import typing as typ

import pytest

from buildlimits.config import SupervisorConfig
from buildlimits.supervisor import Supervisor
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _empty_runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Answer every systemd query with no properties and no load.

    Parameters
    ----------
    argv : Sequence[str]
        The manager query, echoed into the completed process.

    Returns
    -------
    subprocess.CompletedProcess[str]
        A successful run with empty standard output and error.
    """
    return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def supervisor() -> cabc.Iterator[Supervisor]:
    """Build a supervisor over a quiet manager, closing its selector after.

    Yields
    ------
    Supervisor
        A supervisor at a fixed monotonic time, with no cache mode selected.
    """
    manager = UserManager(_empty_runner)
    instance = Supervisor(SupervisorConfig(), manager, clock=lambda: 0.0)
    yield instance
    instance.selector.close()
