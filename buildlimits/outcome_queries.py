"""Bounded sccache statistics and server-generation ports."""

import errno
import os
import shutil
import socket
import struct
import typing as typ
from pathlib import Path

from buildlimits import (
    nonblocking_child,
    outcome_attribution,
    outcome_ports,
    proc_trees,
)
from buildlimits.outcome_process_sampler import ProcessSampler
from buildlimits.outcome_recorder import OUTCOME_QUERY_SECONDS

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_STATISTICS_ARGUMENTS = ("--show-stats", "--stats-format", "json")
_PROCESS_ENVIRONMENT_KEYS = ("PATH", "HOME", "XDG_RUNTIME_DIR")
_MAX_PROC_ENVIRONMENT_BYTES = 64 * 1024
_CONNECT_IN_PROGRESS = frozenset({
    errno.EINPROGRESS,
    errno.EALREADY,
    errno.EWOULDBLOCK,
    errno.EAGAIN,
})


def _statistics_result(
    result: nonblocking_child.ChildResult | None,
) -> outcome_ports.Pending | outcome_ports.StatisticsText | outcome_ports.QueryFailure:
    """Convert one reaped child result to the statistics-port vocabulary.

    Returns
    -------
    Pending | StatisticsText | QueryFailure
        Pending while the process is live, decoded statistics on success, or a
        closed failure reason for exit, output or decoding errors.
    """
    if result is None:
        return outcome_ports.Pending()
    if result.failure == "timeout":
        return outcome_ports.QueryFailure("timeout")
    if result.failure is not None or result.returncode != 0:
        return outcome_ports.QueryFailure("read_error")
    try:
        output = result.output.decode("utf-8")
    except UnicodeError:
        return outcome_ports.QueryFailure("read_error")
    return outcome_ports.StatisticsText(output)


class SccacheStatistics:
    """Run one bounded, asynchronous statistics request at a time.

    Parameters
    ----------
    executable : str
        The configured sccache client name or absolute path, resolved once.
    environment : Mapping[str, str]
        The supervisor environment from which the child whitelist is built.
    socket_path : Path
        The managed server's private Unix socket.
    reaper : nonblocking_child.ChildReaper | None
        A shared bounded child owner, or a new one when omitted.

    Examples
    --------
    ``SccacheStatistics("sccache", env, socket).start(10.0)`` returns a
    ticket with a deadline no later than ``12.0``.
    """

    def __init__(
        self,
        executable: str,
        environment: cabc.Mapping[str, str],
        socket_path: Path,
        *,
        reaper: nonblocking_child.ChildReaper | None = None,
    ) -> None:
        """Resolve the client and retain only its approved environment."""
        resolved = shutil.which(executable, path=environment.get("PATH"))
        self._executable = None if resolved is None else str(Path(resolved).absolute())
        self._environment = {
            key: environment[key]
            for key in _PROCESS_ENVIRONMENT_KEYS
            if key in environment
        }
        self._environment["SCCACHE_SERVER_UDS"] = str(socket_path)
        self._reaper = nonblocking_child.ChildReaper() if reaper is None else reaper
        self._children: dict[
            int, tuple[outcome_ports.Ticket, nonblocking_child.NonBlockingChild]
        ] = {}
        self._next_serial = 1

    def start(self, now: float) -> outcome_ports.Ticket | outcome_ports.QueryFailure:
        """Start the fixed JSON statistics query without waiting for it.

        Parameters
        ----------
        now : float
            Current monotonic time used to establish the query deadline.

        Returns
        -------
        Ticket | QueryFailure
            The opaque ticket, or ``read_error`` when resolution or spawn fails.
        """
        if self._executable is None or self._children:
            return outcome_ports.QueryFailure("read_error")
        deadline = now + OUTCOME_QUERY_SECONDS
        try:
            child = self._reaper.start(
                (self._executable, *_STATISTICS_ARGUMENTS),
                self._environment,
                deadline=deadline,
            )
        except OSError, ValueError:
            return outcome_ports.QueryFailure("read_error")
        if child is None:
            return outcome_ports.QueryFailure("read_error")
        ticket = outcome_ports.Ticket(self._next_serial, deadline)
        self._next_serial += 1
        self._children[ticket.serial] = (ticket, child)
        return ticket

    def poll(
        self, ticket: outcome_ports.Ticket, now: float
    ) -> (
        outcome_ports.Pending
        | outcome_ports.StatisticsText
        | outcome_ports.QueryFailure
    ):
        """Poll the shared reaper and return one bounded terminal result.

        Parameters
        ----------
        ticket : Ticket
            The identity returned by ``start``.
        now : float
            Current monotonic time supplied by the event loop.

        Returns
        -------
        Pending | StatisticsText | QueryFailure
            Pending work or one terminal, bounded reading result.
        """
        owned = self._children.get(ticket.serial)
        if owned is None or owned[0] != ticket:
            return outcome_ports.QueryFailure("read_error")
        self._reaper.poll(now)
        if now >= ticket.deadline:
            return outcome_ports.QueryFailure("timeout")
        return _statistics_result(owned[1].result)

    def close(self, ticket: outcome_ports.Ticket, /) -> None:
        """Cancel an unfinished ticket and let the reaper collect its child.

        Parameters
        ----------
        ticket : Ticket
            A ticket returned by ``start``; closing it repeatedly is safe.
        """
        owned = self._children.get(ticket.serial)
        if owned is None or owned[0] != ticket:
            return
        del self._children[ticket.serial]
        if owned[1].result is None:
            self._reaper.cancel(owned[1])


class PeerServerProcess:
    """Read the managed server identity from its socket peer and ``/proc``.

    Parameters
    ----------
    socket_path : Path
        The managed server's private Unix socket.
    proc_root : Path
        The proc filesystem root; injectable for bounded read-failure tests.

    Examples
    --------
    A refused connection returns ``None`` because no managed server is present.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        proc_root: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        sampler: ProcessSampler | None = None,
    ) -> None:
        """Retain the managed socket and proc root used by D2."""
        self._socket_path = socket_path
        self._proc_root = proc_root
        self._sampler = (
            ProcessSampler(proc_root, cgroup_root) if sampler is None else sampler
        )

    def generation(
        self,
    ) -> outcome_attribution.Generation | outcome_ports.GenerationFailure | None:
        """Read the server PID, process start time and invocation identifier.

        Returns
        -------
        Generation | GenerationFailure | None
            The connected process identity, a bounded read failure, or None
            only when the socket actively refuses the connection.
        """
        try:
            pid = self._peer_pid()
            if pid is None:
                return None
            starttime = self._starttime(pid)
            invocation = self._invocation_id(pid)
        except OSError, ValueError, UnicodeError, struct.error, TimeoutError:
            return outcome_ports.GenerationFailure("generation_unavailable")
        return outcome_attribution.Generation(invocation, pid, starttime)

    def full_emit_metadata(self) -> frozenset[str] | outcome_ports.SampleFailure:
        """Sample bounded full-emit compiler metadata in the server's group.

        Returns
        -------
        frozenset[str] | SampleFailure
            In-memory metadata values or a closed sampling failure.
        """
        generation = self.generation()
        if not isinstance(generation, outcome_attribution.Generation):
            return outcome_ports.SampleFailure("server_unknown")
        return self._sampler.full_emit_metadata(generation)

    def _peer_pid(self) -> int | None:
        """Connect without blocking and return the peer PID from credentials.

        Returns
        -------
        int | None
            The peer PID, or None when the socket refuses a connection.

        Raises
        ------
        OSError
            If the connection or peer credentials cannot be read.
        TimeoutError
            If the non-blocking connect would need a later tick to complete.
        ValueError
            If peer credentials report an invalid process identifier.
        """
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            # A zero timeout makes connect_ex return without blocking.
            peer.settimeout(0.0)
            error = peer.connect_ex(str(self._socket_path))
            if error == errno.ECONNREFUSED:
                return None
            if error in _CONNECT_IN_PROGRESS:
                msg = "managed socket connect is still in progress"
                raise TimeoutError(msg)
            if error not in {0, errno.EISCONN}:
                raise OSError(error, os.strerror(error))
            credentials = peer.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
        pid, _uid, _gid = struct.unpack("3i", credentials)
        if pid <= 0:
            msg = "managed socket returned an invalid peer PID"
            raise ValueError(msg)
        return pid

    def _starttime(self, pid: int) -> int:
        """Read field 22 while respecting the parenthesized command name.

        Returns
        -------
        int
            The process start time in clock ticks.

        A malformed field 22 propagates as ``ValueError`` from the shared
        process-stat parser.
        """
        return proc_trees.read_process_starttime(pid, self._proc_root)

    def _invocation_id(self, pid: int) -> str:
        """Read the optional systemd invocation identifier from proc environ.

        Returns
        -------
        str
            The invocation identifier, or an empty value when it is absent.

        Raises
        ------
        ValueError
            If the environment exceeds its fixed read bound.
        """
        with (self._proc_root / str(pid) / "environ").open("rb") as proc_environment:
            raw_environment = proc_environment.read(_MAX_PROC_ENVIRONMENT_BYTES + 1)
        if len(raw_environment) > _MAX_PROC_ENVIRONMENT_BYTES:
            msg = "process environment exceeded its read bound"
            raise ValueError(msg)
        prefix = b"INVOCATION_ID="
        for variable in raw_environment.split(b"\0"):
            if variable.startswith(prefix):
                return os.fsdecode(variable[len(prefix) :])
        return ""
