"""Scripted journal transports and a native-journal receiver for tests.

The metrics query reaches the journal only through an injected runner, so
these helpers stand in for ``journalctl`` with fixed JSON lines while every
record still passes through the real validation boundary.
:func:`journal_receiver` binds the datagram socket a sink writes native
records to, standing in for ``systemd-journald``.

Examples
--------
>>> import subprocess
>>> result = journal_runner('{"MESSAGE": "x"}')(["journalctl"])
>>> isinstance(result, subprocess.CompletedProcess), result.stdout
(True, '{"MESSAGE": "x"}')
"""

import contextlib
import socket
import subprocess  # ruff: ignore[suspicious-subprocess-import] - builds CompletedProcess results only; nothing is run.
import typing as typ

from buildlimits import metrics_query

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

type JournalRunner = cabc.Callable[
    [cabc.Sequence[str]], subprocess.CompletedProcess[str]
]


def journal_runner(output: str) -> JournalRunner:
    """Provide an injected successful journal transport.

    Parameters
    ----------
    output : str
        The JSON lines the transport returns.

    Returns
    -------
    JournalRunner
        A runner returning ``output`` with a zero exit status.
    """
    return lambda argv: subprocess.CompletedProcess(argv, 0, output, "")


def journal_fields(payload: bytes) -> dict[str, str]:
    r"""Decode one text-only native journal datagram.

    Parameters
    ----------
    payload : bytes
        A datagram received from ``systemd-journald``.

    Returns
    -------
    dict[str, str]
        The journal fields, split at their first equals sign.

    Examples
    --------
    >>> journal_fields(b"MESSAGE=build-limits trace\nPRIORITY=6")
    {'MESSAGE': 'build-limits trace', 'PRIORITY': '6'}
    """
    return dict(
        line.split("=", maxsplit=1) for line in payload.decode("ascii").splitlines()
    )


def fixture_query(
    fixture: Path,
) -> cabc.Callable[[str, int, str], metrics_query.Window]:
    """Build a query that reads a fixture file instead of the journal.

    Parameters
    ----------
    fixture : Path
        JSON lines standing in for the journal's output.

    Returns
    -------
    Callable[[str, int, str], metrics_query.Window]
        A query with the CLI's signature that validates the fixture's records.
    """

    def query(since: str, max_records: int, source: str) -> metrics_query.Window:
        """Read the fixture through the real validation boundary.

        Returns
        -------
        metrics_query.Window
            The validated fixture window.
        """
        return metrics_query.query(
            since=since,
            max_records=max_records,
            source=source,
            runner=journal_runner(fixture.read_text()),
        )

    return query


@contextlib.contextmanager
def journal_receiver(path: Path, timeout: float = 1.0) -> cabc.Iterator[socket.socket]:
    """Bind a datagram receiver where a sink sends its native records.

    Parameters
    ----------
    path : Path
        Where to bind; pass the same path to the sink under test.
    timeout : float
        Seconds a receive waits before failing the test.

    Yields
    ------
    socket.socket
        The bound receiver, closed when the block ends.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(str(path))
        receiver.settimeout(timeout)
        yield receiver
