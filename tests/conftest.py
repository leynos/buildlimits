"""Shared fixtures: a recording metric transport and teardown for owned resources.

The recording fixtures (``metric_sink``, ``metrics``, ``telemetry``) give
compile-admission metric tests a transport that keeps bounded native records.
``socket_pair`` yields a connected stream pair. The teardown fixtures keep a
test from leaving descriptors to the garbage collector: ``recovery_harnesses``
builds recovery supervisors and closes each one's resources, and ``owned``
unwinds whatever a test enters into it, such as sockets, pools and
``contextlib.closing`` supervisors, in reverse order after the test.
"""

import contextlib
import socket
import typing as typ

import pytest
from cache_recovery_harness import HarnessFactory

from buildlimits.metrics import Metrics

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


class RecordingSink:
    """Keep bounded native records for behavioural assertions.

    Attributes
    ----------
    records : list[dict[str, str]]
        Copies of all received records.
    """

    def __init__(self) -> None:
        """Start without records."""
        self.records: list[dict[str, str]] = []

    def emit(self, record: cabc.Mapping[str, str]) -> None:
        """Keep an independent copy of one record.

        Parameters
        ----------
        record : Mapping[str, str]
            Validated journal fields.
        """
        self.records.append(dict(record))

    def of_event(self, event: str) -> list[dict[str, str]]:
        """Select the records of one metric event, in emission order.

        Parameters
        ----------
        event : str
            The ``BUILD_LIMITS_EVENT`` value to keep.

        Returns
        -------
        list[dict[str, str]]
            The matching records.
        """
        return [
            record
            for record in self.records
            if record.get("BUILD_LIMITS_EVENT") == event
        ]


@pytest.fixture
def metric_sink() -> RecordingSink:
    """Provide a fresh record sink.

    Returns
    -------
    RecordingSink
        A transport owned by one test.
    """
    return RecordingSink()


@pytest.fixture
def metrics(metric_sink: RecordingSink) -> Metrics:
    """Provide the supervisor emitter over the recording sink.

    Parameters
    ----------
    metric_sink : RecordingSink
        Destination for emitted records.

    Returns
    -------
    Metrics
        A validating emitter with no diagnostic output.
    """
    return Metrics(metric_sink)


@pytest.fixture
def telemetry(metric_sink: RecordingSink) -> tuple[Metrics, RecordingSink]:
    """Provide one emitter and its recording transport.

    Parameters
    ----------
    metric_sink : RecordingSink
        The test's transport.

    Returns
    -------
    tuple[Metrics, RecordingSink]
        The emitter and sink for one lifecycle test.
    """
    return Metrics(metric_sink), metric_sink


@pytest.fixture
def socket_pair() -> cabc.Iterator[tuple[socket.socket, socket.socket]]:
    """Provide a connected stream pair, closed after the test.

    Yields
    ------
    tuple[socket.socket, socket.socket]
        The requester's end and the supervisor's end of one connection.
    """
    client, server = socket.socketpair()
    yield client, server
    client.close()
    server.close()


@pytest.fixture
def recovery_harnesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> cabc.Iterator[HarnessFactory]:
    """Provide a factory whose supervisors are closed after the test.

    Yields
    ------
    HarnessFactory
        Builds recovery harnesses; the fixture closes every one at teardown,
        so no supervisor leaves its selector's descriptor to the collector.
    """
    with contextlib.ExitStack() as owned:
        yield HarnessFactory(tmp_path, monkeypatch, owned)


@pytest.fixture
def owned() -> cabc.Iterator[contextlib.ExitStack]:
    """Provide a stack that closes whatever a test enters into it.

    Enter a ``contextlib.closing`` supervisor, a socket or any other
    descriptor owner here rather than leaving it to the garbage collector,
    whose timing decides which later test sees the descriptor.

    Yields
    ------
    contextlib.ExitStack
        Unwound in reverse order at teardown, even after a failed test.
    """
    with contextlib.ExitStack() as stack:
        yield stack
