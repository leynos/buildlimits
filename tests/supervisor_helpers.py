"""Scaffolding shared by the supervisor's direct-unit test modules.

Several modules drive :class:`Supervisor` handlers directly and each starts
from the same premise: a supervisor over a systemd manager that answers every
query with nothing, at a fixed clock, with its selector closed when the test
ends. The substitution is identical in every case, so it lives here rather
than being copied between the modules.
"""

import contextlib
import dataclasses as dc
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests script the manager's argv, never a shell.
import typing as typ

import pytest
from cache_unit_fake import FakeClock
from conftest import RecordingSink
from pool_helpers import attach_pool

from buildlimits.config import SupervisorConfig
from buildlimits.metrics import Metrics
from buildlimits.supervisor import Supervisor, SupervisorTelemetry
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from pool_helpers import FakePool

    from buildlimits.metrics import MetricsPort


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


@dc.dataclass(slots=True)
class OneSlotScenario:
    """A one-slot supervisor with a hand-held clock, a pool and a sink.

    Attributes
    ----------
    supervisor : Supervisor
        Supervisor whose effects the test drives directly.
    sink : RecordingSink
        Records the supervisor has published so far.
    clock : FakeClock
        The instant the scenario has reached; write ``clock.time`` to move it.
    pool : FakePool
        Scripted token pool the supervisor reads.
    """

    supervisor: Supervisor
    sink: RecordingSink
    clock: FakeClock
    pool: FakePool


@contextlib.contextmanager
def one_slot_supervisor(
    port_factory: cabc.Callable[[RecordingSink], MetricsPort] = Metrics,
) -> cabc.Generator[OneSlotScenario]:
    """Yield a fixed-clock one-slot supervisor and its collaborators.

    The supervisor is configured for the detached cache path, so no managed
    server is touched, and its selector is closed when the block ends.

    Parameters
    ----------
    port_factory : Callable[[RecordingSink], MetricsPort]
        Builds the emission boundary over the scenario's sink; the default is
        the real validating emitter, and a test that needs a faulty transport
        supplies a wrapper.

    Yields
    ------
    OneSlotScenario
        The scenario a volume or pressure test drives.
    """
    clock = FakeClock()
    sink = RecordingSink()
    instance = Supervisor(
        SupervisorConfig(cache_mode="direct", slots=1),
        clock=lambda: clock.time,
        telemetry=SupervisorTelemetry(metrics=port_factory(sink)),
    )
    pool = attach_pool(instance)
    with contextlib.ExitStack() as owned:
        owned.callback(instance.selector.close)
        # ExitStack runs callbacks in reverse order, so pending requests are
        # refused before the selector that carries their replies closes.
        owned.callback(instance.requests.close_all)
        yield OneSlotScenario(instance, sink, clock, pool)


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
