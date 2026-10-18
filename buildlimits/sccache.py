"""The managed sccache server: health and live compilation count.

ADR 011 keeps sccache in the admitted compile path. The server runs as a
systemd user service on a private Unix socket. Two facts about it drive the
supervisor's decisions:

- whether it is reachable, because a client that cannot reach a server starts
  one of its own inside the compiler's scope, which must never happen;
- how many compilations it is running, because a cache-miss compilation runs
  as a direct child of the server, outside any compiler scope, and must stay
  covered by a held token.
"""

import dataclasses as dc
import socket
import typing as typ
from pathlib import Path, PurePosixPath

from buildlimits import cgroups
from buildlimits.bus_properties import BusQueryError, control_unit
from buildlimits.proc_trees import count_children
from buildlimits.systemd import SystemdError, UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class SccacheError(Exception):
    """The managed server's state cannot be determined."""


class LiveCacheWorkError(SccacheError):
    """A successful manager query observes a live cache with unaccounted work.

    Parameters
    ----------
    roots : int or None
        Visible compiler-root trees at the moment of the query, or None when
        the count could not be established. The count corroborates starvation
        in the automatic recovery trigger; it never authorizes token release,
        because the dangerous wait sits before any compiler exists.
    """

    def __init__(self, message: str, *, roots: int | None = None) -> None:
        """Record the fault message and the visible root count it observed.

        Parameters
        ----------
        message : str
            The operator-facing description of the observed work.
        roots : int or None
            Visible compiler-root trees, or None when the count could not be
            established.
        """
        super().__init__(message)
        self.roots = roots


class StoppedPopulatedCacheError(SccacheError):
    """The managed service is stopped but its control group holds work.

    The cgroup need not name the process that holds the retained work; a
    member may simply be slow to exit. Either way the stopped state cannot
    account for it, so the diagnosis is separate from both a live service
    (which may hold accepted requests awaiting a jobserver token) and a
    manager transport fault.
    """


class StartupAccountingError(SccacheError):
    """Previous cache work requires operator accounting before admission starts."""


def _members(control_group: str) -> list[int]:
    """Read a validated absolute server cgroup path.

    Returns
    -------
    list[int]
        Processes in the verified hierarchy path.

    Raises
    ------
    ValueError
        If the path is relative or traverses a parent directory.
    """
    path = PurePosixPath(control_group)
    if not path.is_absolute() or ".." in path.parts:
        msg = "manager returned an invalid server control group"
        raise ValueError(msg)
    return cgroups.member_pids(cgroups.directory(path))


def _visible_roots(properties: cabc.Mapping[str, str]) -> int:
    """Count visible trees without authorizing detached-work token release.

    Returns
    -------
    int
        Visible compiler-root trees, including orphaned roots.

    """
    main_pid = _server_pid(properties)
    control_group = properties["ControlGroup"]
    if not control_group:
        _verify_no_group(main_pid)
        return 0
    members = _members(control_group)
    _verify_membership(main_pid, members)
    return count_children(main_pid, members) if main_pid else 0


def _server_pid(properties: cabc.Mapping[str, str]) -> int:
    """Require complete server-load properties and a non-negative PID.

    Returns
    -------
    int
        Parsed manager PID, including zero for a stopped server.

    Raises
    ------
    ValueError
        If load properties are missing or the PID is malformed.
    """
    if not {"MainPID", "ControlGroup"} <= properties.keys():
        msg = "manager omitted required server load properties"
        raise ValueError(msg)
    main_pid = int(properties["MainPID"])
    if main_pid < 0:
        msg = "manager returned a negative server PID"
        raise ValueError(msg)
    return main_pid


def _verify_no_group(main_pid: int) -> None:
    """Allow a missing cgroup only for a stopped server.

    Raises
    ------
    ValueError
        If a live server has no verified control group.
    """
    if main_pid != 0:
        msg = "a live server has no verified control group"
        raise ValueError(msg)


def _verify_membership(main_pid: int, members: list[int]) -> None:
    """Require an empty stopped cgroup or a live server inside its own cgroup.

    Raises
    ------
    ValueError
        If the reported server state contradicts cgroup membership.
    """
    if main_pid == 0:
        if members:
            msg = "stopped server control group still contains work"
            raise ValueError(msg)
        return
    if main_pid not in members:
        msg = "server PID is absent from its reported control group"
        raise ValueError(msg)


@dc.dataclass(slots=True)
class ManagedSccache:
    """The managed sccache server, as seen by the supervisor.

    Parameters
    ----------
    unit : str
        The server's systemd user unit.
    socket_path : Path
        The server's private Unix socket.
    manager : UserManager
        The user manager port.
    """

    unit: str
    socket_path: Path
    manager: UserManager = dc.field(default_factory=UserManager)

    def is_reachable(self, timeout: float = 0.5) -> bool:
        """Check that the server accepts connections on its socket.

        Parameters
        ----------
        timeout : float
            Seconds to wait for the connection.

        Returns
        -------
        bool
            Whether a connection succeeded; ``False`` when the socket is
            absent, refuses connections or does not answer in time.

        Raises
        ------
        SccacheError
            If the probe fails for any other reason, such as a permission
            fault, so that a misconfiguration is not mistaken for a stopped
            server.
        """
        probe = None
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(timeout)
            probe.connect(str(self.socket_path))
        except FileNotFoundError, ConnectionRefusedError, TimeoutError:
            return False
        except OSError as error:
            msg = f"cannot probe {self.socket_path}: {error}"
            raise SccacheError(msg) from error
        finally:
            if probe is not None:
                probe.close()
        return True

    def active_compiles(self, *, snapshot: cabc.Mapping[str, str] | None = None) -> int:
        """Count visible compiler trees for diagnostics, without proving drain.

        Parameters
        ----------
        snapshot : Mapping[str, str] or None
            Previously queried manager properties, or None to query the unit.

        Returns
        -------
        int
            Measured compiler-root trees in the verified server cgroup,
            including orphaned work. Stopped units require verified emptiness.

        Raises
        ------
        SccacheError
            If the unit, its control group or its processes cannot be read.
            Callers must then keep holding tokens: an unknown load is not a
            zero load.
        """
        try:
            properties = (
                self.manager.show(self.unit, "MainPID", "ControlGroup")
                if snapshot is None
                else snapshot
            )
            return _visible_roots(properties)
        except (SystemdError, OSError, ValueError) as error:
            msg = f"cannot read {self.unit}'s load: {error}"
            raise SccacheError(msg) from error

    def accounted_drain(self) -> int:
        """Verify that potentially detached server work can no longer start.

        Returns
        -------
        int
            Zero only for a verified stopped and empty service.

        Raises
        ------
        SccacheError
            If the service is live, contains work, or cannot be inspected.
            A live server may hold accepted tasks awaiting a jobserver token;
            zero compiler processes therefore does not prove completion.
        """
        try:
            properties = self.manager.show(
                self.unit, "MainPID", "ControlGroup", "ActiveState"
            )
            self._verify_accounting(properties)
        except (SystemdError, OSError, ValueError) as error:
            msg = f"cannot account for {self.unit}'s detached work: {error}"
            raise SccacheError(msg) from error
        return 0

    def _verify_accounting(self, properties: cabc.Mapping[str, str]) -> None:
        """Reject live, transitioning or nonempty service snapshots.

        Raises
        ------
        ValueError
            If the complete snapshot cannot prove stopped and empty state.
        LiveCacheWorkError
            If a successfully queried live service may retain detached requests.
        """
        if not {"MainPID", "ControlGroup", "ActiveState"} <= properties.keys():
            msg = "manager omitted required drain properties"
            raise ValueError(msg)
        if int(properties["MainPID"]) != 0:
            roots = self._diagnostic_roots(properties)
            shown = "unknown" if roots is None else roots
            msg = (
                "live sccache service may retain detached requests; "
                f"visible compiler roots={shown}; operator accounting required"
            )
            raise LiveCacheWorkError(msg, roots=roots)
        _verify_stopped_service(properties)

    def _diagnostic_roots(self, properties: cabc.Mapping[str, str]) -> int | None:
        """Sample visible work without authorizing token release on any outcome.

        Returns
        -------
        int or None
            Root count, or None when the count could not be established.
        """
        try:
            return self.active_compiles(snapshot=properties)
        except SccacheError:
            return None

    def stop(self) -> None:
        """Ask the manager to queue a stop for the managed server.

        Raises
        ------
        SystemdError
            If the manager refuses the job or cannot be reached. The unit may
            still be live afterwards: only :meth:`accounted_drain` proves that
            it stopped and emptied.
        """  # ruff: ignore[docstring-extraneous-exception] - _control raises it.
        self._control("stop")

    def start(self) -> None:
        """Ask the manager to queue a start for the managed server.

        Raises
        ------
        SystemdError
            If the manager refuses the job or cannot be reached.
        """  # ruff: ignore[docstring-extraneous-exception] - _control raises it.
        self._control("start")

    def _control(self, verb: str) -> None:
        """Run one control verb privately, falling back to the public bus.

        Raises
        ------
        SystemdError
            If neither transport can queue the job. Both faults are reported
            together so the operator sees the whole reason.
        """
        try:
            self.manager.control_unit(verb, self.unit)
        except SystemdError as private_error:
            try:
                control_unit(self.manager.runner, verb, self.unit)
            except (BusQueryError, SystemdError) as public_error:
                msg = (
                    f"cannot {verb} {self.unit}: "
                    f"{ascii(str(private_error)[:300])[1:-1]}; "
                    f"{ascii(str(public_error)[:300])[1:-1]}"
                )
                raise SystemdError(msg) from public_error


def _verify_stopped_service(properties: cabc.Mapping[str, str]) -> None:
    """Require stopped unit state and empty cgroup before detached work is accounted.

    Raises
    ------
    ValueError
        If the stopped state cannot be verified.
    StoppedPopulatedCacheError
        If the service is stopped but its control group still holds processes,
        so surviving work may hold a shared jobserver slot.
    """
    if properties["ActiveState"] not in {"inactive", "failed"}:
        msg = "server unit is not in a verified stopped state"
        raise ValueError(msg)
    if properties["ControlGroup"] and _members(properties["ControlGroup"]):
        msg = "stopped server control group still contains work"
        raise StoppedPopulatedCacheError(msg)
