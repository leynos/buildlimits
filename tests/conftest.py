"""Reusable recording transport for compile-admission metric tests."""

import socket
import typing as typ

import pytest

from buildlimits.metrics import Metrics

if typ.TYPE_CHECKING:
    import collections.abc as cabc


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
